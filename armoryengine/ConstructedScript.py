################################################################################
#                                                                              #
# Copyright (C) 2011-2014, Armory Technologies, Inc.                           #
# Distributed under the GNU Affero General Public License (AGPL v3)            #
# See LICENSE or http://www.gnu.org/licenses/agpl.html                         #
#                                                                              #
################################################################################
from ArmoryUtils import *
from BinaryPacker import *
from BinaryUnpacker import *
from Transaction import getOpCode
from ArmoryEncryption import NULLSBD
from CppBlockUtils import HDWalletCrypto
import re

# First "official" version will be 1. 0 is the prototype version.
BTCID_PKS_VERSION = 0
BTCID_CS_VERSION = 0
BTCID_PKRP_VERSION = 0
BTCID_SRP_VERSION = 0
BTCID_PR_VERSION = 0

BTCID_PAYLOAD_TYPE = enum('KeySource', 'ConstructedScript')
ESCAPECHAR  = '\xff'
ESCESC      = '\x00'

# Use in SignableIDPayload
BTCID_PAYLOAD_BYTE = { \
   BTCID_PAYLOAD_TYPE.KeySource:       '\x00',
   BTCID_PAYLOAD_TYPE.ConstructedScript:  '\x01' }

class VersionError(Exception): pass

try:
   # Normally the decorator simply confirms that function arguments
   # are of the expected type.  Will throw an error if not defined.
   VerifyArgTypes
except:
   # If it's not available, just make a replacement decorator that does nothing
   def VerifyArgTypes(*args, **kwargs):
      def decorator(func):
         return func
      return decorator

################################ Internal Data #################################
################################################################################
class MultiplierProof(object):
   """
   Simply a list of 32-byte multipliers, and a 4-byte fingerprint of both the
   root key where the mults will be applied, and the resultant key. The four
   bytes aren't meant to be cryptographically strong, just data that helps
   reduce unnecessary computation. Mults are obtained from C++.
   """

   #############################################################################
   def __init__(self, isNull=None, srcFinger4=None, dstFinger4=None,
                multList=None):
      self.isNull      = None   # If static, stealth, etc, no mult list
      self.srcFinger4  = None   # Just the first 4B of hash256(root pub key)
      self.dstFinger4  = None   # Just the first 4B of hash256(result pub key)
      self.rawMultList = []     # List of 32-byte LE multipliers

      if isNull is not None:
         self.initialize(isNull, srcFinger4, dstFinger4, multList)


   #############################################################################
   def initialize(self, isNull=None, srcFinger4=None, dstFinger4=None,
                  multList=None):
      self.isNull = isNull
      if isNull:
         self.srcFinger4  = None
         self.dstFinger4  = None
         self.rawMultList = []
      else:
         self.srcFinger4  = srcFinger4
         self.dstFinger4  = dstFinger4
         self.rawMultList = multList[:]


   #############################################################################
   def serialize(self):
      flags = BitSet(8)
      flags.setBit(0, self.isNull)

      bp = BinaryPacker()
      bp.put(BITSET, flags, widthBytes=1)

      if not self.isNull:
         bp.put(BINARY_CHUNK, self.srcFinger4, widthBytes= 4)
         bp.put(BINARY_CHUNK, self.dstFinger4, widthBytes= 4)
         bp.put(VAR_INT, len(self.rawMultList))
         for mult in self.rawMultList:
            bp.put(BINARY_CHUNK,  mult,  widthBytes=32)

      return bp.getBinaryString()


   #############################################################################
   def unserialize(self, serData):
      bu = makeBinaryUnpacker(serData)
      flags = bu.get(BITSET, 1)

      if flags.getBit(0):
         self.initialize(isNull=True)
      else:
         srcFinger4B = bu.get(BINARY_CHUNK, 4)
         dstFinger4B = bu.get(BINARY_CHUNK, 4)
         numMult  = bu.get(VAR_INT)

         multList = []
         for m in numMult:
            multList.append( bu.get(BINARY_CHUNK, 32))

         self.initialize(False, srcFinger4B, dstFinger4B, multList)

      return self


################################################################################
class SignableIDPayload(object):
   """
   This datastructure wraps up all the other classes above into a single,
   embeddable data type.
   """
   #############################################################################
   def __init__(self):
      self.version     = None
      self.createDate  = None
      self.expireDate  = None
      self.payloadType = None  # KeySource or ConstructedScript
      self.payload     = None


   #############################################################################
   def initialize(self, template):
      self.rawTemplate = template


   #############################################################################
   def serialize(self):
      pass


   #############################################################################
   def unserialize(self, templateStr):
      bu = makeBinaryUnpacker(templateStr)

      oplist = []


################################################################################
def DeriveBip32PublicKeyWithProof(startPubKey, binChaincode, indexList):
   """
   We will actually avoid using the higher level ArmoryKeyPair (AKP) objects
   for now, as they do a ton of extra stuff we don't need for this.  We go
   a bit lower-level and talk to CppBlockUtils.HDWalletCrypto directly.

   Inputs:
      startPubKey:   python string, 33-byte compressed public key
      binChaincode:  python string, 32-byte chaincode
      indexList:     python list of UINT32s, anything >0x7fffffff is hardened

   Output: [finalPubKey, proofObject]

      finalPubKey:   pyton string:  33-byte compressed public key
      proofObject:   MultiplierProof: list of 32-byte mults to be applied
                     to the input startPubKey to produce the finalPubKey

   Note that an error will be thrown if any items in the index list correspond
   to a hardened derivation.  We need this proof to be generatable strictly
   from public key material.
   """

   # Sanity check the inputs
   if not len(startPubKey)==33 or not startPubKey[0] in ['\x02','\x03']:
      raise KeyDataError('Input public key is a valid format')

   if not len(binChaincode)==32:
      raise KeyDataError('Chaincode must be 32 bytes')

   # Crypto-related code uses SecureBinaryData and Cpp.ExtendedKey objects
   sbdPublicKey = SecureBinaryData(startPubKey)
   sbdChainCode = SecureBinaryData(binChaincode)
   extPubKeyObj = Cpp.ExtendedKey(sbdPublicKey, sbdChainCode)

   # Prepare the output multiplier list
   binMultList = []

   # Derive the children
   for childIndex in indexList:
      if (childIndex & 0x80000000) > 0:
         raise ChildDeriveError('Cannot generate proofs along hardened paths')

      # Pass in a NULL SecureBinaryData object as a reference
      sbdMultiplier = NULLSBD()

      # Computes the child and emits the multiplier via the last arg
      extPubKeyObj = Cpp.HDWalletCrypto().childKeyDeriv(extPubKeyObj,
                                                        childIndex,
                                                        sbdMultiplier)

      # Append multiplier to list
      binMultList.append(sbdMultiplier.toBinStr())

   finalPubKey = extPubKeyObj.getPublicKey().toBinStr()
   proofObject = MultiplierProof(isNull=False,
                                srcFinger4=hash256(startPubKey)[:4],
                                dstFinger4=hash256(finalPubKey)[:4],
                                multList=binMultList)

   return finalPubKey, proofObject


################################################################################
def ApplyProofToRootKey(startPubKey, multProofObj, expectFinalPub=None):
   """
   Inputs:
      startPubKey:    python string, 33-byte compressed public key
      multProofObj:   MultiplierProof object
      expectFinalPub: Optionally provide the final pub key we expect

   Output: finalPubKey

      finalPubKey:    python string with resulting public key, will match
                      expectFinalPub input if supplied.

   Since we don't expect this to fail, KeyDataError raised on failure
   """
   if not hash256(startPubKey)[:4] == multProofObj.srcFinger4:
      raise KeyDataError('Source fingerprint of proof does not match root pub')

   finalPubKey = HDWalletCrypto().getChildKeyFromOps_SWIG(startPubKey,
                                                          multProofObj.rawMultList)

   if len(finalPubKey) == 0:
      raise KeyDataError('Key derivation failed - Elliptic curve violations')

   if not hash256(finalPubKey)[:4] == multProofObj.dstFinger4:
      raise KeyDataError('Dst fingerprint of proof does not match root pub')

   if expectFinalPub and not finalPubKey==expectFinalPub:
      raise KeyDataError('Computation did not yield expected public key!')

   return finalPubKey


#############################################################################
def makeBinaryUnpacker(inputStr):
   """
   Use this on input args so that unserialize funcs can treat the
   input as a BU object.  If it's not a BU object, convert it, and
   the consumer method will start reading from byte zero.  If it
   is BU, then forward the reference to it so that it starts unserializing
   from the current location in the BU object, leaving the position
   after the data was unserialized.
   """
   if isinstance(inputStr, BinaryUnpacker):
      # Just return the input reference
      return inputStr
   else:
      # Initialize a new BinaryUnpacker
      return BinaryUnpacker(inputStr)


################################################################################
def escapeFF(inputStr):
   """
   Take a string intended for a DANE script template and "escape" it such that
   any instance of 0xff becomes 0xff00. This must be applied to any string that
   will be processed by a DANE script template decoder.
   """
   convExp = re.compile('ff', re.IGNORECASE)
   convStr = convExp.sub('ff00', inputStr)
   return convStr


################################ External Data #################################
################################################################################
class PublicKeySource(object):
   """
   This defines a "source" from where we could get a public key, either to be 
   inserted directly into P2PKH, or to be used as part of a multi-sig or other
   non-standard script. 

   @isStatic:        rawSource is just a single public key
   @useCompr:        use compressed or uncompressed version of pubkey
   @useHash160:      pubKey should be hashed before being placed in a script
   @isStealth:       rawSource is intended to be used as an sx address
   @isUserKey:       user should insert their own key in this slot
   @useExternal:     rawSource is actually a link to another pubkey source
   @isChksumPresent: A four-byte checksum is included.
   """

   #############################################################################
   def __init__(self):
      self.version         = BTCID_PKS_VERSION
      self.isStatic        = False
      self.useCompressed   = False
      self.useHash160      = False
      self.isStealth       = False
      self.isUserKey       = False
      self.isExternalSrc   = False
      self.isChksumPresent = True
      self.rawSource       = None


   #############################################################################
   def getFingerprint(self):
      return hash256(self.rawSource)[:4]


   #############################################################################
   @VerifyArgTypes(isStatic   = bool, 
                   useCompr   = bool, 
                   use160     = bool,
                   isSx       = bool,
                   isUser     = bool,
                   isExt      = bool,
                   src        = [str, unicode],
                   chksumPres = bool,
                   ver        = int)
   def initialize(self, isStatic, useCompr, use160, isSx, isUser, isExt, src,
                  chksumPres, ver=ver=BTCID_PKS_VERSION):
      """
      Set all PKS values.
      """

      # We expect regular public key sources to be binary strings, but external
      # sources may be similar to email addresses which need to be unicode
      if isExt != isinstance(src, unicode):
         raise UnicodeError('Must use str for reg srcs, unicode for external')

      self.version       = ver[:] if ver else BTCID_PKS_VERSION
      self.isStatic      = isStatic
      self.useCompressed = useCompr
      self.useHash160    = use160
      self.isStealth     = isSx
      self.isUserKey     = isUser
      self.isExternalSrc = isExt
      self.rawSource     = toBytes(src)


   #############################################################################
   def isInitialized(self):
      return not (self.rawSource is None or len(self.rawSource) == 0)


   #############################################################################
   def getRawSource(self):
      """
      If this is an external source, then the rawSource might be a unicode
      string.  If it was input as unicode, it was converted into this data
      structure using toBytes(), so we'll return it using toUnicode()
      """
      if self.isExternalSrc:
         return toUnicode(self.rawSource)
      else:
         return self.rawSource


   #############################################################################
   def serialize(self):
      flags = BitSet(16)
      flags.setBit(0, self.isStatic)
      flags.setBit(1, self.useCompressed)
      flags.setBit(2, self.useHash160)
      flags.setBit(3, self.isStealth)
      flags.setBit(4, self.isUserKey)
      flags.setBit(5, self.isExternalSrc)
      flags.setBit(6, self.isChksumPresent)

      inner = BinaryPacker()
      inner.put(UINT8,  self.version)
      inner.put(BITSET, flags, width=2)
      inner.put(VAR_STR,self.rawSource)
      pkData = inner.getBinaryString()

      bp = BinaryPacker()
      bp.put(VAR_STR, pkData)
      if self.isChksumPresent:
         # Place a checksum in the data. Somewhat redundant due to signatures.
         # Still useful because it protects data sent to signer.
         chksum = computeChecksum(pkData, 4)
         bp.put(BINARY_CHUNK, chksum)

      return bp.getBinaryString()


   #############################################################################
   def unserialize(self, serData):
      bu = makeBinaryUnpacker(serData)
      pkData = bu.get(VAR_STR)
      chksum = bu.get(BINARY_CHUNK, 4)

      # verify func returns the up-to-one-byte-corrected version of the input
      pkData = verifyChecksum(pkData, chksum)
      if len(pkData) == 0:
         raise UnserializeError('Error correction on key data failed')

      inner  = BinaryUnpacker(pkData)
      ver    = bu.get(UINT8)
      flags  = bu.get(BITSET, 2)
      rawSrc = bu.get(VAR_STR)

      if flags.getBit(6):
         chksum = bu.get(BINARY_CHUNK, 4)
         dataChunk  = inner.getBinaryString()[:-4]
         compChksum = computeChecksum(dataChunk)
         if chksum != compChksum:
            raise DataError('PKS record checksum does not match real checksum')

      if not ver == BTCID_PKS_VERSION:
         # In the future we will make this more of a warning, not error
         raise VersionError('BTCID version does not match the loaded version')

      self.__init__()
      self.initialize(self, flags.getBit(0),
                            flags.getBit(1),
                            flags.getBit(2),
                            flags.getBit(3),
                            flags.getBit(4),
                            flags.getBit(5),
                            flags.getBit(6),
                            rawSrc,
                            ver=ver)

      return self


################################################################################
class ExternalPublicKeySource(object):
   def __init__(self):
      raise NotImplementedError('Have not implemented external sources yet')


################################################################################
class ConstructedScript(object):
   """
   This defines a script template that will be used, in conjunction with a
   series of Public Key Sources, to define the basic data required to
   reconstruct a payment script. Script Relationship Proofs may be required to
   construct the correct public keys.

   @useP2SH:         Final TxOut script uses P2SH instead of being used as-is.
   @isChksumPresent: A four-byte checksum is included.
   """

   def __init__(self):
      self.version         = BTCID_CS_VERSION
      self.scriptTemplate  = None
      self.pubKeySrcList   = None
      self.useP2SH         = None
      self.pubKeyBundles   = []
      self.isChksumPresent = True


   #############################################################################
   @VerifyArgTypes(scrTemp    = str,
                   pubSrcs    = [list, tuple],
                   useP2SH    = bool,
                   chksumPres = bool,
                   ver        = int)
   def initialize(self, scrTemp, pubSrcs, useP2SH, chksumPres,
                  ver=ver=BTCID_CS_VERSION):
      self.version         = ver[:] if ver else BTCID_CS_VERSION
      self.useP2SH         = useP2SH
      self.isChksumPresent = chksumPres
      self.pubKeyBundles   = []

      self.setTemplateAndPubKeySrcs(scrTemp, pubSrcs)


   #############################################################################
   def setTemplateAndPubKeySrcs(self, scrTemp, pubSrcs):
      """
      Inputs:
         scrTemp:  script template  (ff-escaped)
         pubSrcs:  flat list of PublicKeySource objects

      Outputs:
         Sets member vars self.scriptTemplate and self.pubKeyBundles
         pubkeyBundles will be a list-of-lists as described below.

      Let's say we have a script template like this: this is a non-working
      2-of-3 OR 3-of-5, with the second key list sorted)

      OP_IF 
         OP_2 0xff01 0xff01 0xff01 OP_3 OP_CHECKMULTISIG 
      OP_ELSE 
         OP_3 0xff05 OP_5 OP_CHECKMULTISIG
      OP_ENDIF

      We have 4 public key bundles: first three are of size 1, the last is 5.
      In this script, the five keys in the second half of the script are sorted
      We should end up with:  
   
      Final result sould look like:

             [ [PubSrc1], [PubSrc2], [PubSrc3], [PubSrc4, PubSrc5, ...]]
                   1          2          3       <--------- 4 -------->
      """
      if '\xff\xff' in scrTemp or scrTemp.endswith('\xff'):
         raise BadInputError('All 0xff sequences need to be properly escaped')

      # The first byte after each ESCAPECHAR is number of pubkeys to insert.
      # ESCAPECHAR+'\x00' is interpretted as as single
      # 0xff op code.  i.e.  0xff00 will be inserted in the final 
      # script as a single 0xff byte (which is OP_INVALIDOPCODE).   For the 
      # purposes of this function, 0xff00 is ignored.
      # 0xffff should not exist in any script template
      scriptPieces = scrTemp.split(ESCAPECHAR)

      # Example after splitting:
      # 76a9ff0188acff03ff0001 would now look like:  '76a9' '0188ac' '03', '0001']
      #                                                      ^^       ^^    ^^
      #                                                  ff-escaped chars
      # We want this to look like:                   '76a9',  '88ac',  '',   '01'
      #        with escape codes:                           01       03     ff
      #        with 2 pub key bundles                      [k0] [k1,k2,k3]

      # Get the first byte after every 0xff
      breakoutPairs = [[pc[0],pc[1:]] for pc in scriptPieces[1:]]
      escapedBytes  = [binary_to_int(b[0]) for b in breakoutPairs if b[0]]
      #scriptPieces  = [scriptPieces[0]] + [b[1] for b in bundleBytes]

      if sum(escapedBytes) != len(pubSrcs):
         raise UnserializeError('Template key count do not match pub list size')

      self.scriptTemplate = scrTemp
      self.pubKeySrcList  = pubSrcs[:]
      self.pubKeyBundles  = []

      # Slice up the pubkey src list into the bundles
      idx = 0
      for sz in escapedBytes:
         if sz > 0:
            self.pubKeyBundles.append( self.pubKeySrcList[idx:idx+sz] )
            idx += sz


   #############################################################################
   @staticmethod
   def StandardP2PKHConstructed(binRootPubKey):
      """
      Standard Pay-to-public-key-hash script
      """

      if not len(binRootPubKey) in [33,65]:
         raise KeyDataError('Invalid pubkey;  length=%d' % len(binRootPubKey))

      templateStr  = ''
      templateStr += getOpCode('OP_DUP')
      templateStr += getOpCode('OP_HASH160')
      templateStr += '\xff\x01'
      templateStr += getOpCode('OP_EQUALVERIFY')
      templateStr += getOpCode('OP_CHECKSIG')

      pks = PublicKeySource()
      pks.initialize(isStatic=False,
                     useCompr=(len(binRootPubKey)==33),
                     use160=True,
                     isSx=False,
                     isUser=False,
                     isExt=False,
                     src=binRootPubKey)

      cs = ConstructedScript()
      cs.initialize(self, templateStr, [pks], False)
      return cs


   #############################################################################
   # Check the hash160 call. There were 2 calls, one w/ Hash160 and one w/o.
   @staticmethod
   def StandardP2PKConstructed(binRootPubKey, hash160=False):
      """ This is bare pubkey, usually used with coinbases """
      if not len(binRootPubKey) in [33,65]:
         raise KeyDataError('Invalid pubkey;  length=%d' % len(binRootPubKey))

      templateStr  = ''
      templateStr += '\xff\x01'
      templateStr += getOpCode('OP_CHECKSIG')

      pks = PublicKeySource()
      pks.initialize(isStatic=False,
                     useCompr=(len(binRootPubKey)==33),
                     use160=hash160,
                     isSx=False,
                     isUser=False,
                     isExt=False,
                     src=binRootPubKey)

      cs = ConstructedScript()
      cs.initialize(self, templateStr, [pks], False)
      return cs


   #############################################################################
   @staticmethod
   def StandardMultisigConstructed(M, binRootList):
      # Make sure all keys are valid before processing them.
      for pk in binRootList:
         if not len(pk) in [33,65]:
            raise KeyDataError('Invalid pubkey;  length=%d' % len(pk))
         else:
            sbdPublicKey = SecureBinaryData(pk)
            if not CryptoECDSA().VerifyPublicKeyValid(sbdPublicKey):
               raise KeyDataError('Invalid pubkey received: Key=0x%s' % pk)

      # Make sure there aren't too many keys.
      N = len(binRootList)
      if (not 0 < M <= LB_MAXM):
         raise BadInputError('M value must be less than %d' % LB_MAXM)
      elif (not 0 < N <= LB_MAXN):
         raise BadInputError('N value must be less than %d' % LB_MAXN)

      # Build a template for the standard multisig script.
      templateStr  = ''
      templateStr += getOpCode('OP_%d' % M)
      templateStr += '\xff' + int_to_binary(N, widthBytes=1)
      templateStr += getOpCode('OP_%d' % N)
      templateStr += getOpCode('OP_CHECKMULTISIG')

      pksList = []
      for rootPub in binRootList:
         pks = PublicKeySource()
         pks.initialize(isStatic=False,
                        useCompr=(len(rootPub)==33),
                        use160=False,
                        isSx=False,
                        isUser=False,
                        isExt=False,
                        src=rootPub)
         pksList.append(pks)

      cs = ConstructedScript()
      cs.initialize(self, templateStr, pksList, True)
      return cs


   #############################################################################
   @staticmethod
   def UnsortedMultisigConstructed(M, binRootList):
      """
      THIS PROBABLY WON'T BE USED -- IT IS STANDARD CONVENTION TO ALWAYS SORT!
      Consider this code to be here to illustrate using constructed scripts
      with unsorted pubkey lists.
      """
      # Make sure all keys are valid before processing them.
      for pk in binRootList:
         if not len(pk) in [33,65]:
            raise KeyDataError('Invalid pubkey;  length=%d' % len(pk))
         else:
            sbdPublicKey = SecureBinaryData(pk)
            if not CryptoECDSA().VerifyPublicKeyValid(sbdPublicKey):
               raise KeyDataError('Invalid pubkey received: Key=0x%s' % pk)

      # Make sure there aren't too many keys.
      N = len(binRootList)
      if (not 0 < M <= LB_MAXM):
         raise BadInputError('M value must be less than %d' % LB_MAXM)
      elif (not 0 < N <= LB_MAXN):
         raise BadInputError('N value must be less than %d' % LB_MAXN)

      # Build a template for the standard multisig script.
      templateStr  = ''
      templateStr += getOpCode('OP_%d' % M)
      templateStr += '\xff\x01' * N
      templateStr += getOpCode('OP_%d' % N)
      templateStr += getOpCode('OP_CHECKMULTISIG')

      pksList = []
      for rootPub in binRootList:
         pks = PublicKeySource()
         pks.initialize(isStatic=False,
                        useCompr=(len(rootPub)==33),
                        use160=False,
                        isSx=False,
                        isUser=False,
                        isExt=False,
                        src=rootPub)
         pksList.append(pks)

      cs = ConstructedScript()
      cs.initialize(self, templateStr, pksList, True)
      return cs


   #############################################################################
   def serialize(self):
      flags = BitSet(16)
      flags.setBit(0, self.useP2SH)
      flags.setBit(1, self.isChksumPresent)

      inner = BinaryPacker()
      inner.put(UINT8,   self.version)
      inner.put(BITSET,  flags, width=1)
      inner.put(VAR_STR, self.scriptTemplate)
      inner.put(UINT8,   sum(sum(keyList1) for keyList1 in self.pubKeyBundles)
      for keyList2 in self.pubKeyBundles:
         for keyItem in keyList2:
            inner.put(VAR_STR, keyItem)
      pkData = inner.getBinaryString()

      bp = BinaryPacker()
      bp.put(VAR_STR, pkData)
      if self.isChksumPresent:
         # Place a checksum in the data. Somewhat redundant due to signatures.
         # Still useful because it protects data sent to signer.
         chksum = computeChecksum(pkData, 4)
         bp.put(BINARY_CHUNK, chksum)

      return bp.getBinaryString()


   #############################################################################
   def unserialize(self, serData):
      keyList = []
      bu = makeBinaryUnpacker(serData)
      pkData = bu.get(VAR_STR)
      chksum = bu.get(BINARY_CHUNK, 4)

      # verify func returns the up-to-one-byte-corrected version of the input
      pkData = verifyChecksum(pkData, chksum)
      if len(pkData) == 0:
         raise UnserializeError('Error correction on key data failed')

      inner   = BinaryUnpacker(pkData)
      ver     = bu.get(UINT8)
      flags   = bu.get(BITSET, 1)
      scrTemp = bu.get(VAR_STR)
      numKeys = bu.get(UINT8)
      k = 0
      while k < numKeys:
         nextKey = bu.get(VAR_STR)
         pks = PublicKeySource()
         pks.initialize(isStatic=False,
                        useCompr=(len(nextKey)==33),
                        use160=False,
                        isSx=False,
                        isUser=False,
                        isExt=False,
                        src=nextKey)
         keyList.append(pks)
         k += 1

      if flags.getBit(1):
         dataChunk  = inner.getBinaryString()[:-4]
         compChksum = computeChecksum(dataChunk)
         if chksum != compChksum:
            raise DataError('CS record checksum does not match real checksum')

      if not ver == BTCID_CS_VERSION:
         # In the future we will make this more of a warning, not error
         raise VersionError('BTCID version does not match the loaded version')

      self.__init__()
      initialize(self, scrTemp, pubSrcs, useP2SH, ver=None):
      self.initialize(self, scrTemp,
                            keyList,
                            flags.getBit(0),
                            flags.getBit(1),
                            ver=ver)

      return self


################################################################################
class PublicKeyRelationshipProof(object):
   """
   This defines the actual data that proves how multipliers relate to an
   accompanying public key.
   """

   #############################################################################
   def __init__(self):
      self.version        = BTCID_PKRP_VERSION
      self.numMults       = 0
      self.pubKeyBundles  = []


   #############################################################################
   @VerifyArgTypes(numMults = int,
                   multList = [str, unicode],
                   ver      = int)
   def initialize(self, numMults, multList, ver=BTCID_PKRP_VERSION):
      """
      Set all PKRP values.
      """
      self.version  = ver
      self.numMults = numMults
      self.multList = toBytes(multList)


   #############################################################################
   def isInitialized(self):
      return not (self.multList is None or len(self.multList) == 0)


   #############################################################################
   def serialize(self):
      bp = BinaryPacker()
      bp.put(UINT8,  self.version)
      bp.put(VAR_INT, numMults, width=1)
      for keyList2 in self.pubKeyBundles:
         bp.put(VAR_STR, keyList2)

      return bp.getBinaryString()


   #############################################################################
   def unserialize(self, serData):
      multList = []
      inner  = BinaryUnpacker(serData)
      ver    = bu.get(UINT8)
      numMults = bu.get(VAR_INT, 1)

      k = 0
      while k < numMults:
         nextMult = bu.get(VAR_STR)
         multList.append(nextMult)
         k += 1

      if not ver == BTCID_PKRP_VERSION:
         # In the future we will make this more of a warning, not error
         raise VersionError('BTCID version does not match the loaded version')

      self.__init__()
      self.initialize(self, numMults,
                            multList,
                            ver=ver)

      return self


################################################################################
class ScriptRelationshipProof(object):
   """
   This defines the actual data that proves how multipliers relate to an
   accompanying script.
   """

   #############################################################################
   def __init__(self):
      self.version        = BTCID_SRP_VERSION
      self.numPKRPs       = 0
      self.pkrpBundles  = []


   #############################################################################
   @VerifyArgTypes(numPKRPs = int,
                   pkrpList = [PublicKeyRelationshipProof],
                   ver      = int)
   def initialize(self, numPKRPs, pkrpList, ver=BTCID_SRP_VERSION):
      """
      Set all SRP values.
      """

      self.version  = ver
      self.numPKRPs = numPKRPs
      self.pkrpList = toBytes(pkrpList)


   #############################################################################
   def isInitialized(self):
      return not (self.pkrpList is None or len(self.pkrpList) == 0)


   #############################################################################
   def serialize(self):
      bp = BinaryPacker()
      bp.put(UINT8,  self.version)
      bp.put(VAR_INT, numPKRPs, width=1)
      for pkrpItem in self.pkrpList:
         bp.put(PublicKeyRelationshipProof, pkrpItem)  # Revise this???

      return bp.getBinaryString()


   #############################################################################
   def unserialize(self, serData):
      pkrpList = []
      bu       = BinaryUnpacker(serData)
      ver      = bu.get(UINT8)
      numPKRPs = bu.get(VAR_INT, 1)

      k = 0
      while k < numPKRPs:
         nextPKRP = bu.get(PublicKeyRelationshipProof)
         pkrpList.append(nextPKRP)
         k += 1

      if not ver == BTCID_SRP_VERSION:
         # In the future we will make this more of a warning, not error
         raise VersionError('BTCID version does not match the loaded version')

      self.__init__()
      self.initialize(self, numPKRPs,
                            pkrpList,
                            ver=ver)

      return self


################################################################################
class PaymentRequest(object):
   """
   This defines the actual payment request sent to a paying entity.
   """

   #############################################################################
   def __init__(self):
      self.version            = BTCID_PR_VERSION
      self.numTxOutScripts    = 0
      self.reqSize            = 0
      self.unvalidatedScripts = None
      self.daneReqNames       = None
      self.srpLists           = None


   #############################################################################
   @VerifyArgTypes(numTxOutScripts    = int,
                   reqSize            = int,
                   unvalidatedScripts = [VAR_STR],
                   daneReqNames       = [VAR_STR],
                   srpLists           = [ScriptRelationshipProof],
                   ver                = int)
   def initialize(self, numTxOutScripts, reqSize, unvalidatedScripts,
                  daneReqNames, srpLists, ver=None):
      """
      Set all PR values.
      """
      self.version            = ver
      self.numTxOutScripts    = numTxOutScripts
      self.reqSize            = reqSize
      self.unvalidatedScripts = toBytes(unvalidatedScripts)
      self.daneReqNames       = toBytes(daneReqNames)
      self.srpLists           = toBytes(srpLists)


   #############################################################################
   def isInitialized(self):
      return not (self.unvalidatedScripts is None or
                  len(self.unvalidatedScripts) == 0)


   #############################################################################
   def serialize(self):
      flags = BitSet(16)

      bp = BinaryPacker()
      bp.put(UINT8,  self.version)
      bp.put(BITSET, flags, width=2)
      bp.put(VAR_INT, self.numTxOutScripts, width=3)
      bp.put(VAR_INT, self.reqSize, width=3)
      bp.put(VAR_INT, self.numTxOutScripts, width=3)
      for scriptItem in self.unvalidatedScripts:
         bp.put(VAR_STR, scriptItem)  # Revise this???
      for daneItem in self.daneReqNames:
         bp.put(VAR_STR, daneItem)  # Revise this???
      for srpItem in self.srpLists:
         bp.put(ScriptRelationshipProof, srpItem)  # Revise this???

      return bp.getBinaryString()


   #############################################################################
   def unserialize(self, serData):
      unvalidatedScripts = []
      daneReqNames       = []
      srpList            = []
      bu                 = makeBinaryUnpacker(serData)
      ver                = bu.get(UINT8)
      flags              = bu.get(BitSet, 2)
      numTxOutScripts    = bu.get(VAR_INT, 3)
      reqSize            = bu.get(VAR_INT, 3)

      k = 0
      while k < numTxOutScripts:
         nextScript = bu.get(VAR_STR)
         unvalidatedScripts.append(nextScript)
         k += 1

      l = 0
      while l < numTxOutScripts:
         daneName = bu.get(VAR_STR)
         daneNames.append(nextScript)
         l += 1

      m = 0
      while m < numTxOutScripts:
         nextSRPList = bu.get(ScriptRelationshipProof)
         srpList.append(nextScript)
         m += 1

      if not readVersionInt(ver) == BTCID_PR_VERSION:
         # In the future we will make this more of a warning, not error
         raise VersionError('BTCID version does not match the loaded version')

      self.__init__()
      self.initialize(self, numTxOutScripts,
                            reqSize,
                            unvalidatedScripts,
                            daneReqNames,
                            srpList,
                            ver=ver)

      return self
