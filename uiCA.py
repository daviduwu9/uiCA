#!/usr/bin/python


import importlib
import os
import random
import re
import sys
from collections import Counter, defaultdict, deque, namedtuple
from heapq import heappop, heappush
from itertools import chain, count
from x64_lib import *

sys.path.append(os.path.join(os.path.dirname(__file__), '../XED-to-XML'))
from disas import *

arch = None
clock = 0
allPorts = []
Retire_Width = 4
RB_Width = 224
RS_Width = 97
PreDecode_Width = 5
predecodeDecodeDelay = 3
IQ_Width = 25
nDecoders = 4 #wikichip seems to be wrong
MITE_Width = 5 # width of path from MITE to IDQ
macroFusibleInstrCanBeDecodedAsLastInstr = True # if True, a macro-fusible instr. can be decoded on the last decoder or when the instruction queue is empty
instrWithMoreThan2UopsDecodedAlone = False
pop5CEndsDecodeGroup = True # after pop rsp and pop r12, no other instr. can be decoded in the same cycle
pop5CRequiresComplexDecoder = False
DSB_Width = 6
IDQ_Width = 64
issue_Width = 4
issue_dispatch_delay = 5

class UopProperties:
   def __init__(self, instr, possiblePorts, inputOperands, outputOperands, divCycles=0, isLoadUop=False, isStoreAddressUop=False, isStoreDataUop=False,
                isFirstUopOfInstr=False, isLastUopOfInstr=False):
      self.instr = instr
      self.possiblePorts = possiblePorts
      self.inputOperands = inputOperands
      self.outputOperands = outputOperands
      self.divCycles = divCycles
      self.isLoadUop = isLoadUop
      self.isStoreAddressUop = isStoreAddressUop
      self.isStoreDataUop = isStoreDataUop
      self.isFirstUopOfInstr = isFirstUopOfInstr
      self.isLastUopOfInstr = isLastUopOfInstr


class Uop:
   idx_iter = count()

   def __init__(self, prop, rnd):
      self.idx = next(self.idx_iter)
      self.prop = prop # instance of UopProperties
      self.rnd = rnd # iteration round
      self.actualPort = None
      self.eliminated = False
      self.renamedInputOperands = [] # [op[1] for op in inputOperands] # [(instrInputOperand, renamedInpOperand), ...]
      self.renamedOutputOperands = [] # [op[1] for op in outputOperands]
      self.predecoded = None
      self.addedToIDQ = None
      self.issued = None
      self.readyForDispatch = None
      self.dispatched = None
      self.executed = None
      self.retired = None
      self.retireIdx = None # how many other uops were already retired in the same cycle
      #self.relatedUops = [self]

   def getUnfusedUops(self):
      return [self]

   def __str__(self):
      return 'Uop(idx: {}, rnd: {}, p: {})'.format(self.idx, self.rnd, self.actualPort)


class FusedUop:
   def __init__(self, uops):
      self.uops = uops

   def getUnfusedUops(self):
      return self.uops


class LaminatedUop:
   def __init__(self, fusedUops):
      self.fusedUops = fusedUops

   def getFusedUops(self):
      return self.fusedUops

   def getUnfusedUops(self):
      return [uop for fusedUop in self.getFusedUops() for uop in fusedUop.getUnfusedUops()]


class StackSynchUop(Uop):
   def __init__(self, instr, rnd):
      possiblePorts = (['0','1','5'] if arch in ['CON', 'WOL', 'NHM', 'WSM', 'SNB', 'IVB'] else ['0','1','5','6'])
      prop = UopProperties(instr, possiblePorts, [RegOperand('RSP')], [RegOperand('RSP')], isFirstUopOfInstr=True)
      Uop.__init__(self, prop, rnd)


class Instr:
   def __init__(self, asm, opcode, posNominalOpcode, instrStr, portData, uops, retireSlots, uopsMITE, uopsMS, divCycles, inputRegOperands, inputMemOperands,
                outputRegOperands, outputMemOperands, memAddrOperands, agenOperands, latencies, TP, lcpStall, modifiesStack, mayBeEliminated, complexDecoder,
                nAvailableSimpleDecoders, hasLockPrefix, isBranchInstr, isSerializingInstr, isLoadSerializing, isStoreSerializing, macroFusibleWith,
                macroFusedWithPrevInstr=False, macroFusedWithNextInstr=False):
      self.asm = asm
      self.opcode = opcode
      self.posNominalOpcode = posNominalOpcode
      self.instrStr = instrStr
      self.portData = portData
      self.uops = uops
      self.retireSlots = retireSlots
      self.uopsMITE = uopsMITE
      self.uopsMS = uopsMS
      self.divCycles = divCycles
      self.inputRegOperands = inputRegOperands
      self.inputMemOperands = inputMemOperands
      self.outputRegOperands = outputRegOperands
      self.outputMemOperands = outputMemOperands
      self.memAddrOperands = memAddrOperands
      self.agenOperands = agenOperands
      self.latencies = latencies # latencies[(inOp,outOp)] = l
      self.TP = TP
      self.lcpStall = lcpStall
      self.modifiesStack = modifiesStack # pops or pushes to the stack
      self.mayBeEliminated = mayBeEliminated # a move instruction that may be eliminated
      self.complexDecoder = complexDecoder # requires the complex decoder
      # no. of instr. that can be decoded with simple decoders in the same cycle; only applicable for instr. with complexDecoder == True
      self.nAvailableSimpleDecoders = nAvailableSimpleDecoders
      self.hasLockPrefix = hasLockPrefix
      self.isBranchInstr = isBranchInstr
      self.isSerializingInstr = isSerializingInstr
      self.isLoadSerializing = isLoadSerializing
      self.isStoreSerializing = isStoreSerializing
      self.macroFusibleWith = macroFusibleWith
      self.macroFusedWithPrevInstr = macroFusedWithPrevInstr
      self.macroFusedWithNextInstr = macroFusedWithNextInstr
      self.UopPropertiesList = [] # list with UopProperties for each (unfused domain) uop
      self.requiresStackSynchUop = False

   def __repr__(self):
       return "Instr: " + str(self.__dict__)

   def generateUops(self, rnd):
      if not self.UopPropertiesList:
         return []

      fusedDomainUops = deque()
      for i in range(0, self.retireSlots-1):
         fusedDomainUops.append(FusedUop([Uop(self.UopPropertiesList[i], rnd)]))
      fusedDomainUops.append(FusedUop([Uop(prop, rnd) for prop in self.UopPropertiesList[self.retireSlots-1:]]))
      if self.requiresStackSynchUop:
         fusedDomainUops.append(StackSynchUop(self, rnd))

      laminatedDomainUops = []
      for _ in range(0, min(self.uopsMITE + self.uopsMS, len(fusedDomainUops)) - 1):
         laminatedDomainUops.append(LaminatedUop([fusedDomainUops.popleft()]))
      laminatedDomainUops.append(LaminatedUop(fusedDomainUops))

      return laminatedDomainUops


class UnknownInstr(Instr):
   def __init__(self, asm, opcode, posNominalOpcode):
      Instr.__init__(self, asm, opcode, posNominalOpcode, instrStr='', portData={}, uops=0, retireSlots=1, uopsMITE=1, uopsMS=0, divCycles=0,
                     inputRegOperands=[], inputMemOperands=[], outputRegOperands=[], outputMemOperands=[], memAddrOperands=[], agenOperands=[], latencies={},
                     TP=None, lcpStall=False, modifiesStack=False, mayBeEliminated=False, complexDecoder=False, nAvailableSimpleDecoders=None,
                     hasLockPrefix=False, isBranchInstr=False, isSerializingInstr=False, isLoadSerializing=False, isStoreSerializing=False,
                     macroFusibleWith=set())


class RegOperand:
   def __init__(self, reg):
      self.reg = reg

class MemOperand:
   def __init__(self, memAddr):
      self.memAddr = memAddr

'''
class InstrInstance:
   def __init__(self, instr):
      self.instr = instr
      self.uops = []
      self.renamedInputOperands = []
      self.renamedOutputOperands = []
'''

class RenamedOperand:
   def __init__(self, nonRenamedOperand=None, complete=True):
      self.nonRenamedOperand = nonRenamedOperand
      self.uops = [] # list of uops that need to have executed before this operand becomes ready
      self.__complete = complete
      self.__ready = None # cycle in which operand becomes ready

   def setUops(self, uops):
      self.uops = uops
      self.__complete = True

   def isComplete(self):
      return self.__complete

   def getReadyCycle(self):
      if not self.isComplete():
         return None
      if self.__ready is not None:
         return self.__ready
      if not self.uops:
         self.__ready = -1
         return self.__ready

      if any((uop.dispatched is None) for uop in self.uops):
         return None

      firstDispatchCycle = min(uop.dispatched for uop in self.uops)
      lastDispatchCycle = max(uop.dispatched for uop in self.uops)
      readyCycle = lastDispatchCycle + 1
      for uop in self.uops:
         for inpOp, renInpOp in zip(uop.prop.inputOperands, uop.renamedInputOperands):
            #if uop.prop.possiblePorts == ['2', '3']:
            #   print str(uop.rnd) + ' ' + str(inpOp) + ' ' + str(renInpOp) + ' ' + str(renInpOp.getReadyCycle())
            if renInpOp.getReadyCycle() is None:
               return None
            lat = uop.prop.instr.latencies.get((inpOp, self.nonRenamedOperand), 1)
            readyCycle = max(readyCycle, firstDispatchCycle + lat, renInpOp.getReadyCycle() + lat)
      self.__ready = readyCycle
      return self.__ready


class Renamer:
   def __init__(self):
      self.renameDict = {}

      # renamed operands written by current instr.; this is necessary because we we generally don't know which uop of an instruction writes an operand
      self.curInstrRndRenameDict = {}
      self.curInstrRndUopsForRenamedOpDict = {}

      self.initValue = 0
      self.abstractValueGenerator = count(1)
      self.abstractValueDict = {'RSP': next(self.abstractValueGenerator), 'RBP': next(self.abstractValueGenerator)}
      self.curInstrRndAbstractValueDict = {}

      self.nGPRMoveEliminationsInPrevCycle = 0
      self.multiUseGPRDict = {}
      self.multiUseGPRDictUseInCycle = {}

      self.nSIMDMoveEliminationsInPrevCycle = 0
      self.multiUseSIMDDict = {}
      self.multiUseSIMDDictUseInCycle = {}

      self.renamerActiveCycle = 0

   def cycle(self, uops):
      self.renamerActiveCycle += 1

      nGPRMoveEliminations = 0
      nSIMDMoveEliminations = 0

      for fusedUop in uops:
         for uop in fusedUop.getUnfusedUops():
            if uop.prop.instr.mayBeEliminated and (not isinstance(uop, StackSynchUop)):
               canonicalInpReg = getCanonicalReg(uop.prop.instr.inputRegOperands[0].reg)
               canonicalOutReg = getCanonicalReg(uop.prop.instr.outputRegOperands[0].reg)

               if (canonicalInpReg in GPRegs):
                  nGPRMoveEliminationsPossible = (4 - nGPRMoveEliminations - self.nGPRMoveEliminationsInPrevCycle
                                                    - self.multiUseGPRDictUseInCycle.get(self.renamerActiveCycle-2, 0))
                  if nGPRMoveEliminationsPossible > 0:
                     uop.eliminated = True
                     nGPRMoveEliminations += 1
                     curMultiUseDict = self.multiUseGPRDict
               elif ('MM' in canonicalInpReg):
                  nSIMDMoveEliminationsPossible = (4 - nSIMDMoveEliminations - self.nSIMDMoveEliminationsInPrevCycle
                                                     - self.multiUseSIMDDictUseInCycle.get(self.renamerActiveCycle-2, 0))
                  if nSIMDMoveEliminationsPossible > 0:
                     uop.eliminated = True
                     nSIMDMoveEliminations += 1
                     curMultiUseDict = self.multiUseSIMDDict

               if uop.eliminated:
                  renamedReg = self.renameDict.setdefault(canonicalInpReg, RenamedOperand())
                  self.curInstrRndRenameDict[canonicalOutReg] = renamedReg
                  if not renamedReg in curMultiUseDict:
                     curMultiUseDict[renamedReg] = set()
                  curMultiUseDict[renamedReg].update([canonicalInpReg, canonicalOutReg])

            if not uop.eliminated:
               if uop.prop.instr.uops or isinstance(uop, StackSynchUop):
                  for inpOp in uop.prop.inputOperands:
                     key = self.getRenameDictKey(inpOp)
                     renamedOp = self.renameDict.setdefault(key, RenamedOperand(inpOp))
                     uop.renamedInputOperands.append(renamedOp)
                  for outOp in uop.prop.outputOperands:
                     key = self.getRenameDictKey(outOp)
                     if key not in self.curInstrRndRenameDict:
                        renOp = RenamedOperand(outOp, complete=False)
                        self.curInstrRndRenameDict[key] = renOp
                        self.curInstrRndAbstractValueDict[key] = self.computeAbstractValue(outOp, uop.prop.instr)
                        #print str(key) + ' ' + str(self.curInstrRndAbstractValueDict[key])
                     renamedOp = self.curInstrRndRenameDict[key]
                     uopsForOp = self.curInstrRndUopsForRenamedOpDict.setdefault(renamedOp, [])
                     uop.renamedOutputOperands.append(renamedOp)
                     uopsForOp.append(uop)
               else:
                  # e.g., xor rax, rax
                  for op in uop.prop.instr.outputRegOperands:
                     self.curInstrRndRenameDict[getCanonicalReg(op.reg)] = RenamedOperand()

            if uop.prop.isLastUopOfInstr:
               for renOp, uopsForOp in self.curInstrRndUopsForRenamedOpDict.items():
                  renOp.setUops(uopsForOp)

               for key in self.curInstrRndRenameDict:
                  if key in self.renameDict:
                     prevRenOp = self.renameDict[key]
                     if (not uop.eliminated) or (prevRenOp != self.curInstrRndRenameDict[key]):
                        if (key in GPRegs) and (prevRenOp in self.multiUseGPRDict):
                           self.multiUseGPRDict[prevRenOp].remove(key)
                        elif ('MM' in key) and (prevRenOp in self.multiUseSIMDDict):
                           self.multiUseSIMDDict[prevRenOp].remove(key)

               self.renameDict.update(self.curInstrRndRenameDict)
               self.abstractValueDict.update(self.curInstrRndAbstractValueDict)
               self.curInstrRndRenameDict.clear()
               self.curInstrRndUopsForRenamedOpDict.clear()
               self.curInstrRndAbstractValueDict.clear()

      self.nGPRMoveEliminationsInPrevCycle = nGPRMoveEliminations
      self.nSIMDMoveEliminationsInPrevCycle = nSIMDMoveEliminations

      multiUseGPRUse = sum(1 for v in self.multiUseGPRDict.values() if v)
      if multiUseGPRUse:
         self.multiUseGPRDictUseInCycle[self.renamerActiveCycle] = multiUseGPRUse
      for k, v in list(self.multiUseGPRDict.items()):
         if len(v) <= 0:
            del self.multiUseGPRDict[k]

      multiUseSIMDUse = sum(1 for v in self.multiUseSIMDDict.values() if v)
      if multiUseSIMDUse:
         self.multiUseSIMDDictUseInCycle[self.renamerActiveCycle] = multiUseSIMDUse
      for k, v in list(self.multiUseSIMDDict.items()):
         if len(v) <= 1:
            del self.multiUseSIMDDict[k]

      return uops

   def getRenameDictKey(self, op, agen=False):
      if isinstance(op, RegOperand):
         return getCanonicalReg(op.reg) # ToDo: partial register stalls
      else:
         memAddr = op.memAddr
         return (self.abstractValueDict.get(memAddr.base, self.initValue), self.abstractValueDict.get(memAddr.index, self.initValue), memAddr.scale,
                 memAddr.displacement, agen)

   def getAbstractValue(self, op, agen=False):
      key = self.getRenameDictKey(op, agen)
      if not key in self.abstractValueDict:
         if not agen:
            self.abstractValueDict[key] = self.initValue
         else:
            self.abstractValueDict[key] = next(self.abstractValueGenerator)
      return self.abstractValueDict[key]

   def computeAbstractValue(self, outOp, instr):
      if 'MOV' in instr.instrStr and not 'CMOV' in instr.instrStr:
         if instr.inputMemOperands:
            return self.getAbstractValue(instr.inputMemOperands[0])
         elif instr.inputRegOperands:
            return self.getAbstractValue(instr.inputRegOperands[0])
         else:
            return next(self.abstractValueGenerator)
      elif instr.instrStr in ['POP (R16)', 'POP (R64)', 'POP (M16)', 'POP (M64)']:
         return self.getAbstractValue(instr.inputMemOperands[0])
      elif instr.instrStr.startswith('LEA_'):
         return self.getAbstractValue(instr.agenOperands[0], agen=True)
      else:
         return next(self.abstractValueGenerator)


class FrontEnd:
   def __init__(self, instructions, reorderBuffer, scheduler, unroll):
      self.IDQ = deque()
      self.renamer = Renamer()
      self.reorderBuffer = reorderBuffer
      self.scheduler = scheduler
      self.unroll = unroll

      self.MS = MicrocodeSequencer()

      instructionQueue = deque()
      self.preDecoder = PreDecoder(instructionQueue)
      self.decoder = Decoder(instructionQueue, self.MS)

      self.DSB = DSB(self.MS)
      self.addressesInDSB = set()

      self.uopSource = 'MITE'
      if unroll:
         self.cacheBlockGenerator = CacheBlockGenerator(instructions, True)
      else:
         self.cacheBlocksForNextRoundGenerator = CacheBlocksForNextRoundGenerator(instructions)
         cacheBlocksForFirstRound = next(self.cacheBlocksForNextRoundGenerator)
         self.findCacheableAddresses(cacheBlocksForFirstRound)
         for cacheBlock in cacheBlocksForFirstRound:
            self.addNewCacheBlock(cacheBlock)
         if 0 in self.addressesInDSB:
            self.uopSource = 'DSB'

   def cycle(self):
      renamerUops = []
      if len(self.IDQ) >= issue_Width and not self.reorderBuffer.isFull() and not self.scheduler.isFull(): # the first check seems to be wrong, but leads to better results
         while self.IDQ:
            lamUop = self.IDQ[0]
            #if (lamUop.getUnfusedUops()[0].idx == 0) and (len(self.IDQ) < IDQ_Width / 2):
            #   break

            if any((uop.prop.isFirstUopOfInstr and uop.prop.instr.isSerializingInstr) for uop in lamUop.getUnfusedUops()) and not self.reorderBuffer.isEmpty():
               break
            fusedUops = lamUop.getFusedUops()
            if len(renamerUops) + len(fusedUops) > issue_Width:
               break
            renamerUops.extend(fusedUops)
            self.IDQ.popleft()

      issueUops = []
      if renamerUops:
         issueUops = self.renamer.cycle(renamerUops)
         for fusedUop in issueUops:
            for uop in fusedUop.getUnfusedUops():
               uop.issued = clock

      self.reorderBuffer.cycle(issueUops)
      self.scheduler.cycle(issueUops)

      if len(self.IDQ) + DSB_Width > IDQ_Width:
         return

      # add new cache blocks
      while len(self.DSB.B32BlockQueue) < 2 and len(self.preDecoder.B16BlockQueue) < 4:
         if self.unroll:
            self.addNewCacheBlock(next(self.cacheBlockGenerator))
         else:
            for cacheBlock in next(self.cacheBlocksForNextRoundGenerator):
               self.addNewCacheBlock(cacheBlock)

      # add new uops to IDQ
      newUops = []
      if self.MS.isBusy():
         newUops = self.MS.cycle()
      elif self.uopSource == 'MITE':
         self.preDecoder.cycle()
         newInstrIUops = self.decoder.cycle()
         newUops = [u for _, u in newInstrIUops if u is not None]
         if not self.unroll and newInstrIUops:
            curInstrI = newInstrIUops[-1][0]
            if curInstrI.instr.isBranchInstr or curInstrI.instr.macroFusedWithNextInstr:
               if 0 in self.addressesInDSB:
                  self.uopSource = 'DSB'
      elif self.uopSource == 'DSB':
         newInstrIUops = self.DSB.cycle()
         newUops = [u for _, u in newInstrIUops if u is not None]
         if newInstrIUops and not self.DSB.isBusy():
            curInstrI = newInstrIUops[-1][0]
            if curInstrI.instr.isBranchInstr or curInstrI.instr.macroFusedWithNextInstr:
               nextAddr = 0
            else:
               nextAddr = curInstrI.address + len(curInstrI.instr.opcode)/2
            if nextAddr not in self.addressesInDSB:
               self.uopSource = 'MITE'

      for lamUop in newUops:
         self.IDQ.append(lamUop)
         for uop in lamUop.getUnfusedUops():
            uop.addedToIDQ = clock


   def findCacheableAddresses(self, cacheBlocksForOneRound):
      for cacheBlock in cacheBlocksForOneRound:
         B32Blocks = [block for block in split64ByteBlockTo32ByteBlocks(cacheBlock) if block]
         if all(self.canBeCached(block) for block in B32Blocks):
            # on SKL, a 64-Byte block cannot be cached if the first or the second 32 Bytes cannot be cached
            # ToDo: other microarchitectures
            for B32Block in B32Blocks:
               self.addressesInDSB.add(B32Block[0].address)

   def canBeCached(self, B32Block):
      if sum(len(instrI.uops) for instrI in B32Block if not instrI.instr.macroFusedWithPrevInstr) > 18:
         # a 32-Byte block cannot be cached if it contains more than 18 uops
         return False
      lastInstrI = B32Block[-1]
      if lastInstrI.instr.macroFusedWithNextInstr:
         return False
      if lastInstrI.instr.isBranchInstr and (lastInstrI.address % 32) + len(lastInstrI.instr.opcode)/2 >= 32:
         # on SKL, if the next instr. after a branch starts in a new block, the current block cannot be cached
         # ToDo: other microarchitectures
         return False
      return True

   def addNewCacheBlock(self, cacheBlock):
      B32Blocks = split64ByteBlockTo32ByteBlocks(cacheBlock)
      for B32Block in B32Blocks:
         if not B32Block: continue
         if B32Block[0].address in self.addressesInDSB:
            d = deque(instrI for instrI in B32Block if not instrI.instr.macroFusedWithPrevInstr)
            if d:
               self.DSB.B32BlockQueue.append(d)
         else:
            for B16Block in split32ByteBlockTo16ByteBlocks(B32Block):
               if not B16Block: continue
               self.preDecoder.B16BlockQueue.append(deque(B16Block))
               lastInstrI = B16Block[-1]
               if lastInstrI.instr.isBranchInstr and (lastInstrI.address % 16) + len(lastInstrI.instr.opcode)/2 > 16:
                  # branch instr. ends in next block
                  self.preDecoder.B16BlockQueue.append(deque())



class DSB:
   def __init__(self, MS):
      self.MS = MS
      self.B32BlockQueue = deque()
      self.busy = False

   def cycle(self):
      self.busy = True
      B32Block = self.B32BlockQueue[0]

      retList = []
      #while B32Block and (len(retList) < DSB_Width):
      self.addUopsToList(B32Block, retList)

      if not B32Block:
         self.B32BlockQueue.popleft()
         self.busy = False

         if self.B32BlockQueue and (len(retList) < DSB_Width):
            prevInstrI = retList[-1][0]
            if prevInstrI.address + len(prevInstrI.instr.opcode)/2 == self.B32BlockQueue[0][0].address: # or prevInstrI.instr.isBranchInstr or prevInstrI.instr.macroFusedWithNextInstr:
               self.busy = True
               B32Block = self.B32BlockQueue[0]
               #while B32Block and (len(retList) < DSB_Width):
               self.addUopsToList(B32Block, retList)

               if not B32Block:
                  self.B32BlockQueue.popleft()
                  self.busy = False

      return retList

   def addUopsToList(self, B32Block, lst):
      while B32Block and (len(lst) < DSB_Width):
         instrI = B32Block.popleft()
         lamUops = instrI.uops
         if instrI.instr.uopsMITE:
            for lamUop in lamUops[:instrI.instr.uopsMITE]:
               lst.append((instrI, lamUop))
         else:
            lst.append((instrI, None))
         if instrI.instr.uopsMS:
            self.MS.addUops(lamUops[instrI.instr.uopsMITE:])
            break

   def isBusy(self):
      return self.busy


class MicrocodeSequencer:
   def __init__(self):
      self.uopQueue = deque()
      self.stalled = 0

   def cycle(self):
      uops = []
      if self.stalled:
         self.stalled -= 1
      elif self.uopQueue:
         while self.uopQueue and len(uops) < 4:
            uops.append(self.uopQueue.popleft())
         if not self.uopQueue:
            self.stalled = 1
      return uops

   def addUops(self, uops):
      self.uopQueue.extend(uops)
      self.stalled = 1

   def isBusy(self):
      return self.uopQueue or self.stalled



class Decoder:
   def __init__(self, instructionQueue, MS):
      self.instructionQueue = instructionQueue
      self.MS = MS

   def cycle(self):
      uopsList = []
      nDecodedInstrs = 0
      remainingDecoderSlots = nDecoders
      while self.instructionQueue:
         instrI = self.instructionQueue[0]
         if instrI.instr.macroFusedWithPrevInstr:
            continue
         if instrI.uops[0].getUnfusedUops()[0].predecoded + predecodeDecodeDelay > clock:
            break
         if uopsList and instrI.instr.complexDecoder:
            break
         if instrI.instr.macroFusibleWith and (not macroFusibleInstrCanBeDecodedAsLastInstr):
            if nDecodedInstrs == nDecoders-1:
               break
            if (len(self.instructionQueue) <= 1) or (self.instructionQueue[1].uops[0].getUnfusedUops()[0].predecoded + predecodeDecodeDelay > clock):
               break
         if instrI.instr.macroFusibleWith and ():
            break
         self.instructionQueue.popleft()

         if instrI.instr.uopsMITE:
            for lamUop in instrI.uops[:instrI.instr.uopsMITE]:
               uopsList.append((instrI, lamUop))
         else:
            uopsList.append((instrI, None))

         if instrI.instr.uopsMS:
            self.MS.addUops(instrI.uops[instrI.instr.uopsMITE:])
            break

         if instrI.instr.complexDecoder:
            remainingDecoderSlots = min(remainingDecoderSlots - 1, instrI.instr.nAvailableSimpleDecoders)
         else:
            remainingDecoderSlots -= 1
         nDecodedInstrs += 1
         if remainingDecoderSlots <= 0:
            break
         if instrI.instr.isBranchInstr or instrI.instr.macroFusedWithNextInstr:
            break

      return uopsList

   def isEmpty(self):
      return (not self.instructionQueue)


class PreDecoder:
   def __init__(self, instructionQueue):
      self.B16BlockQueue = deque() # a deque of 16 Byte blocks (i.e., deques of InstrInstances)
      self.instructionQueue = instructionQueue
      self.preDecQueue = deque() # instructions are queued here before they are added to the instruction queue after all stalls have been resolved
      self.stalled = 0
      self.partialInstrI = None

   def cycle(self):
      if not self.stalled:
         if (not self.preDecQueue) and (self.B16BlockQueue or self.partialInstrI) and len(self.instructionQueue) + PreDecode_Width <= IQ_Width:
            if self.partialInstrI is not None:
               self.preDecQueue.append(self.partialInstrI)
               self.partialInstrI = None

            if self.B16BlockQueue:
               curBlock = self.B16BlockQueue[0]

               while curBlock and len(self.preDecQueue) < PreDecode_Width:
                  if instrInstanceCrosses16ByteBoundary(curBlock[0]):
                     break
                  self.preDecQueue.append(curBlock.popleft())

               if len(curBlock) == 1:
                  instrI = curBlock[0]
                  if instrInstanceCrosses16ByteBoundary(instrI):
                     offsetOfNominalOpcode = (instrI.address % 16) + instrI.instr.posNominalOpcode
                     if (len(self.preDecQueue) < 5) or (offsetOfNominalOpcode >= 16):
                        self.partialInstrI = instrI
                        curBlock.popleft()

               if not curBlock:
                  self.B16BlockQueue.popleft()

            self.stalled = sum(3 for ii in self.preDecQueue if ii.instr.lcpStall)

         if not self.stalled:
            for instrI in self.preDecQueue:
               for lamUop in instrI.uops:
                  for uop in lamUop.getUnfusedUops():
                     uop.predecoded = clock
               self.instructionQueue.append(instrI)
            self.preDecQueue.clear()

      self.stalled = max(0, self.stalled-1)

   def isEmpty(self):
      return (not self.B16BlockQueue) and (not self.preDecQueue) and (not self.partialInstrI)

class ReorderBuffer:
   def __init__(self, retireQueue):
      self.uops = deque()
      self.retireQueue = retireQueue

   def isEmpty(self):
      return not self.uops

   def isFull(self):
      return len(self.uops) + issue_Width > RB_Width

   def cycle(self, newUops):
      self.retireUops()
      self.addUops(newUops)

   def retireUops(self):
      nRetiredInSameCycle = 0
      for _ in range(0, Retire_Width):
         if not self.uops: break
         fusedUop = self.uops[0]
         unfusedUops = fusedUop.getUnfusedUops()
         if all((u.executed is not None and u.executed < clock) for u in unfusedUops):
            self.uops.popleft()
            self.retireQueue.append(fusedUop)
            for u in unfusedUops:
               u.retired = clock
               u.retireIdx = nRetiredInSameCycle
            nRetiredInSameCycle += 1
         else:
            break

   def addUops(self, newUops):
      for fusedUop in newUops:
         self.uops.append(fusedUop)
         for uop in fusedUop.getUnfusedUops():
            if (not uop.prop.possiblePorts) or uop.eliminated:
               uop.executed = clock


class Scheduler:
   def __init__(self):
      self.uops = set()
      self.portUsage = {p:0  for p in allPorts}
      self.uopsDispatchedInPrevCycle = [] # the port usage counter is decreased one cycle after uops are issued
      self.divBusy = 0
      self.readyQueue = {p:[] for p in allPorts}
      self.readyDivUops = []
      self.dependentUops = {}
      self.uopsReadyInCycle = {}
      self.nonReadyUops = [] # uops not yet added to uopsReadyInCycle (in order)
      self.pendingUops = set()
      self.pendingStoreFenceUops = deque()
      self.storeUopsSinceLastStoreFence = []
      self.pendingLoadFenceUops = deque()
      self.loadUopsSinceLastLoadFence = []
      self.blockedResources = dict() # for how many remaining cycle a resource will be blocked

   def isFull(self):
      return len(self.uops) + issue_Width > RS_Width

   def cycle(self, newUops):
      self.divBusy = max(0, self.divBusy-1)
      if clock in self.uopsReadyInCycle:
         for uop in self.uopsReadyInCycle[clock]:
            if uop.prop.divCycles:
               heappush(self.readyDivUops, (uop.idx, uop))
            else:
               heappush(self.readyQueue[uop.actualPort], (uop.idx, uop))
         del self.uopsReadyInCycle[clock]

      self.addNewUops(newUops)
      self.dispatchUops()
      self.processNonReadyUops()
      self.processPendingUops()
      self.processPendingFences()
      self.updateBlockedResources()

   def dispatchUops(self):
      uopsDispatched = []
      for port in allPorts:
         queue = self.readyQueue[port]
         if port == '0' and (not self.divBusy) and self.readyDivUops and ((not self.readyQueue['0']) or self.readyDivUops[0][0] < self.readyQueue['0'][0][0]):
            queue = self.readyDivUops
         if (not queue) and port in ['2', '3']:
            queue = self.readyQueue['2' if port == '3' else '3']
         if not queue:
            continue

         uop = heappop(queue)[1]

         uop.actualPort = port
         uop.dispatched = clock
         #uop.executed = clock + 2
         uopsDispatched.append(uop)
         self.divBusy += uop.prop.divCycles
         self.uops.remove(uop)
         self.pendingUops.add(uop)

      for uop in self.uopsDispatchedInPrevCycle:
         self.portUsage[uop.actualPort] -= 1
      self.uopsDispatchedInPrevCycle = uopsDispatched


   def processPendingUops(self):
      for uop in list(self.pendingUops):
         finishTime = uop.dispatched + 2
         if uop.prop.isFirstUopOfInstr and (uop.prop.instr.TP is not None):
            finishTime = max(finishTime, uop.dispatched + uop.prop.instr.TP)
         notFinished = False
         for renOutOp in uop.renamedOutputOperands:
            if not renOutOp.isComplete():
               notFinished = True
               break
            if uop == renOutOp.uops[-1]:
               readyCycle = renOutOp.getReadyCycle()
               if readyCycle is None:
                  notFinished = True
                  break
               finishTime = max(finishTime, readyCycle)
         if notFinished:
            continue
         self.pendingUops.remove(uop)
         uop.executed = finishTime


   def processPendingFences(self):
      for queue, uopsSinceLastFence in [(self.pendingLoadFenceUops, self.loadUopsSinceLastLoadFence),
                                        (self.pendingStoreFenceUops, self.storeUopsSinceLastStoreFence)]:
         if queue:
            executedCycle = queue[0].executed
            if (executedCycle is not None) and executedCycle <= clock:
               queue.popleft()
               del uopsSinceLastFence[:]


   def processNonReadyUops(self):
      newReadyUops = set()
      for uop in self.nonReadyUops:
         if self.checkUopReady(uop):
            newReadyUops.add(uop)
      self.nonReadyUops = [u for u in self.nonReadyUops if (u not in newReadyUops)]


   def updateBlockedResources(self):
      for r in self.blockedResources.keys():
         self.blockedResources[r] = max(0, self.blockedResources[r] - 1)

   # adds ready uops to self.uopsReadyInCycle
   def checkUopReady(self, uop):
      if uop.readyForDispatch is not None:
         return True

      if uop.prop.instr.isLoadSerializing:
         if uop.prop.isFirstUopOfInstr and (self.pendingLoadFenceUops[0] != uop or
                                               any((uop2.executed is None) or (uop2.executed > clock) for uop2 in self.loadUopsSinceLastLoadFence)):
            return False
      elif uop.prop.instr.isStoreSerializing:
         if uop.prop.isFirstUopOfInstr and (self.pendingLoadFenceUops[0] != uop or
                                               any((uop2.executed is None) or (uop2.executed > clock) for uop2 in self.storeUopsSinceLastStoreFence)):
            return False
      else:
         if uop.prop.isLoadUop and self.pendingLoadFenceUops and self.pendingLoadFenceUops[0].idx < uop.idx:
            return False
         if (uop.prop.isStoreDataUop or uop.prop.isStoreAddressUop) and self.pendingStoreFenceUops and self.pendingStoreFenceUops[0].idx < uop.idx:
            return False

      if uop.prop.isFirstUopOfInstr and self.blockedResources.get(uop.prop.instr.instrStr, 0) > 0:
         return False

      readyForDispatchCycle = self.getReadyForDispatchCycle(uop)
      if readyForDispatchCycle is None:
         return False

      uop.readyForDispatch = readyForDispatchCycle
      self.uopsReadyInCycle.setdefault(readyForDispatchCycle, []).append(uop)

      if uop.prop.isFirstUopOfInstr and (uop.prop.instr.TP is not None):
         self.blockedResources[uop.prop.instr.instrStr] = uop.prop.instr.TP

      if uop.prop.isLoadUop:
         self.loadUopsSinceLastLoadFence.append(uop)
      if uop.prop.isStoreDataUop or uop.prop.isStoreAddressUop:
         self.storeUopsSinceLastStoreFence.append(uop)

      return True


   def addNewUops(self, newUops):
      #print len(newUops)
      prevPortUsage = dict(self.portUsage)
      for issueSlot, fusedUop in enumerate(newUops):
         for uop in fusedUop.getUnfusedUops():
            if (not uop.prop.possiblePorts) or uop.eliminated:
               continue
            applicablePorts = [(p,u) for p, u in prevPortUsage.items() if p in uop.prop.possiblePorts]
            minPort, minPortUsage = min(applicablePorts, key=lambda x: (x[1], -int(x[0]))) # port with minimum usage so far

            if issueSlot % 2 == 0 or len(applicablePorts) == 1:
               port = minPort
            else:
               remApplicablePorts = [(p, u) for p, u in applicablePorts if p != minPort]
               min2Port, min2PortUsage = min(remApplicablePorts, key=lambda x: (x[1], -int(x[0]))) # port with second smallest usage so far
               if min2PortUsage >= minPortUsage + 3:
                  port = minPort
               else:
                  port = min2Port

            uop.actualPort = port
            self.portUsage[port] += 1
            self.uops.add(uop)

            #for renInpOp in uop.renamedInputOperands:
            #   for uop2 in renInpOp.uops:
            #      if uop2.dispatched is None:
            #         self.dependentUops.setdefault(uop2, set()).add(uop)

            #if not self.checkUopReady(uop):
            self.nonReadyUops.append(uop)

            if uop.prop.isFirstUopOfInstr:
               if uop.prop.instr.isStoreSerializing:
                  self.pendingStoreFenceUops.append(uop)
               if uop.prop.instr.isLoadSerializing:
                  self.pendingLoadFenceUops.append(uop)

   def getFinishTimeEstimate(self, uop):
      if any((renOutOp.getReadyCycle() is None) for renOutOp in uop.renamedOutputOperands):
         return None
      finishTime = uop.dispatched + 1
      for renOutOp in uop.renamedOutputOperands:
         finishTime = max(finishTime, renOutOp.getReadyCycle())
      return finishTime

   def getReadyForDispatchCycle(self, uop):
      opReadyCycle = -1
      for renInpOp in uop.renamedInputOperands:
         if uop.prop.isLoadUop and isinstance(renInpOp.nonRenamedOperand, MemOperand):
            # load uops can issue as soon as the address registers are ready, before the actual memory is ready
            continue
         if renInpOp.getReadyCycle() is None:
            return None
         opReadyCycle = max(opReadyCycle, renInpOp.getReadyCycle())

      readyCycle = opReadyCycle
      if opReadyCycle < uop.issued + issue_dispatch_delay:
         readyCycle = uop.issued + issue_dispatch_delay
      elif (opReadyCycle == uop.issued + issue_dispatch_delay) or (opReadyCycle == uop.issued + issue_dispatch_delay + 1):
         readyCycle = opReadyCycle + 1

      return max(clock + 1, readyCycle)


def getAllPorts():
   if arch in ['CON', 'WOL', 'NHM', 'WSM', 'SNB', 'IVB']: return [str(i) for i in range(0,6)]
   elif arch in ['HSW', 'BDW', 'SKL', 'SKX', 'KBL', 'CFL', 'CNL']: return [str(i) for i in range(0,8)]
   elif arch in ['ICL']: return [str(i) for i in range(0,10)]


# must only be called once for a given list of instructions
def adjustLatencies(instructions):
   prevWriteToReg = dict() # reg -> instr
   high8RegClean = {'RAX': False, 'RBX': False, 'RCX': False, 'RDX': False}

   def processInstrRegOutputs(instr):
      for outOp in instr.outputRegOperands:
         canonicalOutReg = getCanonicalReg(outOp.reg)
         if instr.mayBeEliminated and instr.instrStr in ['MOV_89 (R64, R64)', 'MOV_8B (R64, R64)']: # ToDo: what if not actually eliminated?
            prevWriteToReg[canonicalOutReg] = prevWriteToReg.get(getCanonicalReg(instr.inputRegOperands[0].reg), instr)
         else:
            prevWriteToReg[canonicalOutReg] = instr

         if canonicalOutReg in ['RAX', 'RBX', 'RCX', 'RDX']:
            if outOp.reg in High8Regs:
               high8RegClean[canonicalOutReg] = True
            elif getRegSize(outOp.reg) > 8:
               high8RegClean[canonicalOutReg] = False

   for instr in instructions:
      processInstrRegOutputs(instr)
   for instr in instructions:
      for inOp in instr.inputMemOperands:
         memAddr = inOp.memAddr
         if arch in ['SNB', 'IVB', 'HSW', 'BDW', 'SKL', 'KBL', 'CFL', 'SKX']:
            if (memAddr.base is not None) and (memAddr.index is None) and (0 <= memAddr.displacement < 2048):
               canonicalBaseReg = getCanonicalReg(memAddr.base)
               if (canonicalBaseReg in prevWriteToReg) and (prevWriteToReg[canonicalBaseReg].instrStr in ['MOV (R64, M64)', 'MOV (RAX, M64)',
                                                                                                          'MOV (R32, M32)', 'MOV (EAX, M32)',
                                                                                                          'MOVSXD (R64, M32)']):
                  for memAddrOp in instr.memAddrOperands:
                     for outputOp in instr.outputRegOperands + instr.outputMemOperands:
                        instr.latencies[(memAddrOp, outputOp)] -= 1
         for outputOp in instr.outputRegOperands:
            instr.latencies[(inOp, outputOp)] -= 3 #ToDo: only on HSW

      if instr.hasLockPrefix:
         for inOp in instr.inputRegOperands:
            # the latency upper bound in the xml file is usually too pessimistic in these cases
            instr.latencies[(inOp, instr.outputMemOperands[0])] = instr.latencies[(inOp, instr.outputRegOperands[0])]

      if any((not high8RegClean[getCanonicalReg(inOp.reg)]) for inOp in instr.inputRegOperands if inOp.reg in High8Regs):
         for key in list(instr.latencies.keys()):
            instr.latencies[key] += 1

      processInstrRegOutputs(instr)


def computeUopProperties(instructions):
   stackPtrImplicitlyModified = False
   for instr in instructions:
      allRegOperands = instr.inputRegOperands + instr.outputRegOperands + instr.memAddrOperands
      if any((getCanonicalReg(op.reg) == 'RSP') for op in allRegOperands):
         stackPtrImplicitlyModified = False
      if instr.modifiesStack:
         stackPtrImplicitlyModified = True

   for instr in instructions:
      if instr.macroFusedWithPrevInstr:
         continue

      allRegOperands = instr.inputRegOperands + instr.outputRegOperands + instr.memAddrOperands

      if stackPtrImplicitlyModified and any((getCanonicalReg(op.reg) == 'RSP') for op in allRegOperands):
         instr.requiresStackSynchUop = True
         stackPtrImplicitlyModified = False
      if instr.modifiesStack:
         stackPtrImplicitlyModified = True

      allInputOperands = instr.inputRegOperands + instr.memAddrOperands + instr.inputMemOperands

      loadPcs = []
      storeAddressPcs = []
      storeDataPcs = []
      nonMemPcs = []
      for pc, n in instr.portData.items():
         ports = list(pc)
         if any ((p in ports) for p in ['7', '8']):
            storeAddressPcs.extend([ports]*n)
         elif any((p in ports) for p in ['2', '3']):
            loadPcs.extend([ports]*n)
         elif any((p in ports) for p in ['4', '9']):
            storeDataPcs.extend([ports]*n)
         else:
            nonMemPcs.extend([ports]*n)

      if storeDataPcs and (not storeAddressPcs):
         for _ in range(0, min(len(storeDataPcs), len(loadPcs))):
            storeAddressPcs.append(loadPcs.pop())

      instr.UopPropertiesList = []
      onlyNonMemPcs = (not loadPcs) and (not storeAddressPcs) and (not storeDataPcs)
      allLatencies = list(set(instr.latencies.values()))
      if onlyNonMemPcs and (len(nonMemPcs) == 2) and (len(instr.outputRegOperands) == 1) and (len(allLatencies) == 2):
         # e.g., setnbe (r8), cmovnz (r64, r64)
         outOp = instr.outputRegOperands[0]
         inOps1 = [op for op in instr.inputRegOperands if instr.latencies.get((op, outOp), 1) == allLatencies[0]]
         inOps2 = [op for op in instr.inputRegOperands if op not in inOps1]
         instr.UopPropertiesList.append(UopProperties(instr, nonMemPcs[0], inOps1, [outOp]))
         instr.UopPropertiesList.append(UopProperties(instr, nonMemPcs[1], inOps2, [outOp]))
      else:
         for pc in loadPcs:
            applicableInputOperands = instr.memAddrOperands + instr.inputMemOperands
            applicableOutputOperands = instr.outputRegOperands + instr.outputMemOperands
            instr.UopPropertiesList.append(UopProperties(instr, pc, applicableInputOperands, applicableOutputOperands, isLoadUop=True))
         for pc in storeAddressPcs:
            applicableInputOperands = instr.memAddrOperands
            applicableOutputOperands = instr.outputRegOperands + instr.outputMemOperands
            instr.UopPropertiesList.append(UopProperties(instr, pc, applicableInputOperands, applicableOutputOperands, isStoreAddressUop=True))
         for pc in storeDataPcs:
            applicableInputOperands = allInputOperands
            applicableOutputOperands = instr.outputMemOperands
            instr.UopPropertiesList.append(UopProperties(instr, pc, applicableInputOperands, applicableOutputOperands, isStoreDataUop=True))

         lat1OutputRegs = [] # output register operands that have a latency of at most 1 from all input registers
         lat1InputOperands = set() # input operands that have a latency of 1 to the output operands in lat1OutputRegs
         for outOp in instr.outputRegOperands:
            if all(instr.latencies.get((inOp, outOp), 2) <= 1 for inOp in allInputOperands):
               lat1OutputRegs.append(outOp)
               lat1InputOperands.update(inOp for inOp in allInputOperands if instr.latencies.get((inOp, outOp), 2) == 1)

         nonLat1OutputOperands = instr.outputRegOperands + instr.outputMemOperands
         divCyclesAdded = False
         for i, pc in enumerate(nonMemPcs):
            if (i == 0) and (len(nonMemPcs) > 1) and lat1OutputRegs:
               applicableInputOperands = list(lat1InputOperands)
               applicableOutputOperands = lat1OutputRegs
               nonLat1OutputOperands = [op for op in nonLat1OutputOperands if not op in lat1OutputRegs]
            else:
               applicableInputOperands = allInputOperands
               applicableOutputOperands = nonLat1OutputOperands

            divCycles = 0
            if instr.divCycles and not divCyclesAdded and pc == ['0']:
               divCycles = instr.divCycles
               divCyclesAdded = True

            instr.UopPropertiesList.append(UopProperties(instr, pc, applicableInputOperands, applicableOutputOperands, divCycles))

      for _ in range(0, instr.retireSlots - len(instr.UopPropertiesList)):
         uopProp = UopProperties(instr, None, [], [])
         instr.UopPropertiesList.append(uopProp)

      instr.UopPropertiesList[0].isFirstUopOfInstr = True
      instr.UopPropertiesList[-1].isLastUopOfInstr = True


def getInstructions(filename, rawFile, iacaMarkers, instrDataDict):
   xedBinary = os.path.join(os.path.dirname(__file__), '..', 'XED-to-XML', 'obj', 'wkit', 'bin', 'xed')
   output = subprocess.check_output([xedBinary, '-64', '-v', '4', ('-ir' if rawFile else '-i'), filename])
   disas = parseXedOutput(output, iacaMarkers)

   instructions = []
   for instrD in disas:
      usedRegs = [getCanonicalReg(r) for _, r in instrD.regOperands.items() if r in GPRegs or 'MM' in r]
      sameReg = (len(usedRegs) > 1 and len(set(usedRegs)) == 1)
      usesIndexedAddr = any((getMemAddr(memOp).index is not None) for memOp in instrD.memOperands.values())
      posNominalOpcode = int(instrD.attributes.get('POS_NOMINAL_OPCODE', 0))
      lcpStall = ('PREFIX66' in instrD.attributes) and (instrD.attributes.get('IMM_WIDTH', '') == '16')
      modifiesStack = any(('STACK' in r) for r in instrD.regOperands.values())
      isBranchInstr = any(True for n, r in instrD.regOperands.items() if ('IP' in r) and ('W' in instrD.rw[n]))
      isSerializingInstr = (instrD.iform in ['LFENCE', 'CPUID', 'IRET', 'IRETD', 'RSM', 'INVD', 'INVEPT_GPR64_MEMdq', 'INVLPG_MEMb', 'INVVPID_GPR64_MEMdq',
                                             'LGDT_MEMs64', 'LIDT_MEMs64', 'LLDT_MEMw', 'LLDT_GPR16', 'LTR_MEMw', 'LTR_GPR16', 'MOV_CR_CR_GPR64',
                                             'MOV_DR_DR_GPR64', 'WBINVD', 'WRMSR'])
      isLoadSerializing = (instrD.iform in ['MFENCE', 'LFENCE'])
      isStoreSerializing = (instrD.iform in ['MFENCE', 'SFENCE'])

      instruction = None
      for instrData in instrDataDict.get(instrD.iform, []):
         if all(instrD.attributes.get(k, '0') == v for k, v in instrData['attributes'].items()):
            uops = instrData.get('uops', 0)
            retireSlots = instrData.get('retSlots', 0)
            uopsMITE = instrData.get('uopsMITE', 0)
            uopsMS = instrData.get('uopsMS', 0)
            latData = instrData.get('lat', dict())
            portData = instrData.get('ports', {})
            divCycles = instrData.get('divC', {})
            complexDecoder = instrData.get('complDec', False)
            nAvailableSimpleDecoders = instrData.get('sDec', nDecoders)
            hasLockPrefix = ('locked' in instrData)
            TP = instrData.get('TP')
            if sameReg:
               uops = instrData.get('uops_SR', uops)
               retireSlots = instrData.get('retSlots_SR', retireSlots)
               uopsMITE = instrData.get('uopsMITE_SR', uopsMITE)
               uopsMS = instrData.get('uopsMS_SR', uopsMS)
               latData = instrData.get('lat_SR', latData)
               portData = instrData.get('ports_SR', portData)
               divCycles = instrData.get('divC_SR',divCycles)
               complexDecoder = instrData.get('complDec_SR', complexDecoder)
               nAvailableSimpleDecoders = instrData.get('sDec_SR', nAvailableSimpleDecoders)
               TP = instrData.get('TP_SR', TP)
            elif usesIndexedAddr:
               uops = instrData.get('uops_I', uops)
               retireSlots = instrData.get('retSlots_I', retireSlots)
               uopsMITE = instrData.get('uopsMITE_I', uopsMITE)
               uopsMS = instrData.get('uopsMS_I', uopsMS)
               portData = instrData.get('ports_I', portData)
               divCycles = instrData.get('divC_I',divCycles)
               complexDecoder = instrData.get('complDec_I', complexDecoder)
               nAvailableSimpleDecoders = instrData.get('sDec_I', nAvailableSimpleDecoders)
               TP = instrData.get('TP_I', TP)

            instrInputRegOperands = [(n,r) for n, r in instrD.regOperands.items() if (not 'IP' in r) and (not 'STACK' in r) and (('R' in instrD.rw[n])
                                                                                                        #or ('CW' in instrD.rw[n]) #or (getRegSize(r) in [8, 16]))]
                                                                                                        or any(n==k[0] for k in latData.keys()))]

            instrInputMemOperands = [(n,m) for n, m in instrD.memOperands.items() if ('R' in instrD.rw[n]) or ('CW' in instrD.rw[n])]
            instrOutputRegOperands = [(n, r) for n, r in instrD.regOperands.items() if (not 'IP' in r) and (not 'STACK' in r) and ('W' in instrD.rw[n])]
            instrOutputMemOperands = [(n, m) for n, m in instrD.memOperands.items() if 'W' in instrD.rw[n]]
            instrOutputOperands = instrOutputRegOperands + instrOutputMemOperands

            mayBeEliminated = ('MOV' in instrData['string']) and (not uops) and (len(instrInputRegOperands) == 1) and (len(instrOutputRegOperands) == 1)
            if mayBeEliminated:
               uops = instrData.get('uops_SR', uops)
               portData = instrData.get('ports_SR', portData)

            inputRegOperands = []
            inputMemOperands = []
            outputRegOperands = []
            outputMemOperands = []
            memAddrOperands = []
            agenOperands = []

            outputOperandsDict = dict()
            for n, r in instrOutputRegOperands:
               regOp = RegOperand(r)
               outputRegOperands.append(regOp)
               outputOperandsDict[n] = regOp
            for n, m in instrOutputMemOperands:
               memOp = MemOperand(getMemAddr(m))
               outputMemOperands.append(memOp)
               outputOperandsDict[n] = memOp

            latencies = dict()
            for inpN, inpR in instrInputRegOperands:
               if (not mayBeEliminated) and all(latData.get((inpN, o), 1) == 0 for o, _ in instrOutputOperands): # e.g., zero idioms
                  continue
               regOp = RegOperand(inpR)
               inputRegOperands.append(regOp)
               for outN, _ in instrOutputOperands:
                  latencies[(regOp, outputOperandsDict[outN])] = latData.get((inpN, outN), 1)

            for inpN, inpM in instrInputMemOperands:
               memOp = MemOperand(getMemAddr(inpM))
               if 'AGEN' in inpN:
                  agenOperands.append(memOp)
               else:
                  inputMemOperands.append(memOp)
                  for outN, _ in instrOutputOperands:
                     latencies[(memOp, outputOperandsDict[outN])] = latData.get((inpN, outN, 'mem'), 1)

            if not modifiesStack:
               for inpN, inpM in set(instrInputMemOperands + instrOutputMemOperands):
                  memAddr = getMemAddr(inpM)
                  for reg, addrType in [(memAddr.base, 'addr'), (memAddr.index, 'addrI')]:
                     if (reg is None) or ('IP' in reg): continue
                     regOp = RegOperand(reg)
                     memAddrOperands.append(regOp)
                     for outN, _ in instrOutputOperands:
                        latencies[(regOp, outputOperandsDict[outN])] = latData.get((inpN, outN, addrType), 1)

            if (not complexDecoder) and (uopsMS or (uopsMITE + uopsMS > 1)):
               complexDecoder = True

            if instrData['string'] in ['POP (R16)', 'POP (R64)'] and instrD.opcode.endswith('5C'):
               complexDecoder |= pop5CRequiresComplexDecoder
               if pop5CEndsDecodeGroup:
                  nAvailableSimpleDecoders = 0

            instruction = Instr(instrD.asm, instrD.opcode, posNominalOpcode, instrData['string'], portData, uops, retireSlots, uopsMITE, uopsMS, divCycles,
                                inputRegOperands, inputMemOperands, outputRegOperands, outputMemOperands, memAddrOperands, agenOperands, latencies, TP,
                                lcpStall, modifiesStack, mayBeEliminated, complexDecoder, nAvailableSimpleDecoders, hasLockPrefix, isBranchInstr,
                                isSerializingInstr, isLoadSerializing, isStoreSerializing, instrData.get('macroFusible', set()))
            #print instruction
            break

      if instruction is None:
         instruction = UnknownInstr(instrD.asm, instrD.opcode, posNominalOpcode)

      # Macro-fusion
      if instructions:
         prevInstr = instructions[-1]
         if instruction.instrStr in prevInstr.macroFusibleWith:
            instruction.macroFusedWithPrevInstr = True
            prevInstr.macroFusedWithNextInstr = True
            instrPorts = instruction.portData.keys()[0]
            if prevInstr.uops == 0:
               prevInstr.uops = instruction.uops
               prevInstr.portData = instruction.portData
            else:
               for p, u in prevInstr.portData.items():
                  if set(instrPorts).issubset(set(p)):
                     del prevInstr.portData[p]
                     prevInstr.portData[instrPorts] = u
                     break

      instructions.append(instruction)
   return instructions


InstrInstance = namedtuple('InstrInstance', ['instr', 'address', 'round', 'uops'])

def split64ByteBlockTo16ByteBlocks(cacheBlock):
   return [[ii for ii in cacheBlock if b*16 <= ii.address % 64 < (b+1)*16 ] for b in range(0,4)]

def split32ByteBlockTo16ByteBlocks(B32Block):
   return [[ii for ii in B32Block if b*16 <= ii.address % 32 < (b+1)*16 ] for b in range(0,2)]

def split64ByteBlockTo32ByteBlocks(cacheBlock):
   return [[ii for ii in cacheBlock if b*32 <= ii.address % 64 < (b+1)*32 ] for b in range(0,2)]

def instrInstanceCrosses16ByteBoundary(instrI):
   instrLen = len(instrI.instr.opcode)/2
   return (instrI.address % 16) + instrLen > 16

# returns list of instrInstances corresponding to a 64-Byte cache block
def CacheBlockGenerator(instructions, unroll):
   cacheBlock = []
   nextAddr = 0
   for rnd in count():
      for instr in instructions:
         cacheBlock.append(InstrInstance(instr, nextAddr, rnd, instr.generateUops(rnd)))

         if (not unroll) and instr == instructions[-1]:
            yield cacheBlock
            cacheBlock = []
            nextAddr = 0
            continue

         prevAddr = nextAddr
         nextAddr = prevAddr + len(instr.opcode)/2
         if prevAddr / 64 != nextAddr / 64:
            yield cacheBlock
            cacheBlock = []


# returns cache blocks for one round (without unrolling)
def CacheBlocksForNextRoundGenerator(instructions):
   cacheBlocks = []
   prevRnd = 0
   for cacheBlock in CacheBlockGenerator(instructions, unroll=False):
      curRnd = cacheBlock[-1].round
      if prevRnd != curRnd:
         yield cacheBlocks
         cacheBlocks = []
         prevRnd = curRnd
      cacheBlocks.append(cacheBlock)


def printPortUsage(instructions, uopsForRound):
   formatStr = '|' + '{:^9}|'*(len(allPorts)+1)

   print '-'*(1+10*(len(allPorts)+1))
   print formatStr.format('Uops', *allPorts)
   print '-'*(1+10*(len(allPorts)+1))
   portUsageC = Counter(uop.actualPort for uopsDict in uopsForRound for uops in uopsDict.values() for uop in uops)
   portUsageL = [('{:.2f}'.format(float(portUsageC[p])/len(uopsForRound)) if p in portUsageC else '') for p in allPorts]
   #print formatStr.format(str(sum(len(uops) for uops in uopsForRound[0].values())), *portUsageL)
   print formatStr.format(str(sum(instr.uops for instr in instructions if not instr.macroFusedWithPrevInstr)), *portUsageL)
   print '-'*(1+10*(len(allPorts)+1))
   print ''

   print formatStr.format('Uops', *allPorts)
   print '-'*(1+10*(len(allPorts)+1))
   for instr in instructions:
      uopsForInstr = [uopsDict[instr] for uopsDict in uopsForRound]
      portUsageC = Counter(uop.actualPort for uops in uopsForInstr for uop in uops)
      portUsageL = [('{:.2f}'.format(float(portUsageC[p])/len(uopsForRound)) if p in portUsageC else '') for p in allPorts]

      uopsCol = str(instr.uops)
      if isinstance(instr, UnknownInstr):
         uopsCol = 'X'
      elif instr.macroFusedWithPrevInstr:
         uopsCol = 'M'

      print formatStr.format(uopsCol, *portUsageL) + ' ' + instr.asm


def writeHtmlFile(filename, title, head, body):
   with open(filename, "w") as f:
      f.write('<html>\n'
              '<head>\n'
              '<title>' + title + '</title>\n'
              + head +
              '</head>\n'
              '<body>\n'
              + body +
              '</body>\n'
              '</html>\n')


def generateHTMLTraceTable(filename, instructions, uopsForRound, maxCycle):
   style = []
   style.append('<style>')
   style.append('table {border-collapse: collapse}')
   style.append('table, td, th {border: 1px solid black}')
   style.append('th {text-align: left; padding: 6px}')
   style.append('td {text-align: center}')
   style.append('code {white-space: nowrap}')
   style.append('</style>')
   table = []
   table.append('<table>')
   table.append('<tr>')
   table.append('<th rowspan="2">It.</th>')
   table.append('<th rowspan="2">Instruction</th>')
   table.append('<th colspan="2" style="text-align:center">&mu;ops</th>')
   table.append('<th rowspan="2" colspan="{}">Cycles</th>'.format(maxCycle+1))
   table.append('</tr>')
   table.append('<tr>')
   table.append('<th style="text-align:center">Possible Ports</th>')
   table.append('<th style="text-align:center">Actual Port</th>')
   table.append('</tr>')

   nRows = sum(max(len(uops),1) for uops in uopsForRound[0].values())
   for rnd, uopsDict in enumerate(uopsForRound):
      table.append('<tr style="border-top: 2px solid black">')
      table.append('<td rowspan="{}">{}</td>'.format(nRows, rnd))
      for instrI, instr in enumerate(instructions):
         if instrI > 0:
            table.append('<tr>')
         table.append('<td rowspan=\"{}\" style="text-align:left"><code>{}</code></td>'.format(len(uopsDict[instr]), instr.asm))
         for uopI, uop in enumerate(uopsDict[instr]):
            if uopI > 0:
               table.append('<tr>')
            table.append('<td>{{{}}}</td>'.format(','.join(uop.prop.possiblePorts) if uop.prop.possiblePorts else '-'))
            table.append('<td>{}</td>'.format(uop.actualPort if uop.actualPort else '-'))

            uopEvents = ['' for _ in range(0,maxCycle+1)]
            for evCycle, ev in [(uop.predecoded, 'P'), (uop.addedToIDQ, 'Q'), (uop.issued, 'I'), (uop.readyForDispatch, 'r'), (uop.dispatched, 'D'), (uop.executed, 'E'), (uop.retired, 'R'),
                                (max(op.getReadyCycle() for op in uop.renamedInputOperands) if uop.renamedInputOperands else 0, 'i'),
                                (max(op.getReadyCycle() for op in uop.renamedOutputOperands) if uop.renamedOutputOperands else 0, 'o') ]:
               if evCycle is not None and evCycle <= maxCycle:
                  uopEvents[evCycle] += ev

            for ev in uopEvents:
               table.append('<td>{}</td>'.format(ev))

            table.append('</tr>')

         if not uopsDict[instr]:
            table.append('<td>-</td><td>-</td>')
            table.append('<td></td>'*(maxCycle+1))
            table.append('</tr>')

   table.append('</table>')
   writeHtmlFile(filename, 'Trace', '\n'.join(style), '\n'.join(table))


# Disassembles a binary and finds for each instruction the corresponding entry in the XML file.
# With the -iacaMarkers option, only the parts of the code that are between the IACA markers are considered.
def main():
   parser = argparse.ArgumentParser(description='Disassembler')
   parser.add_argument('filename', help="File to be disassembled")
   parser.add_argument("-iacaMarkers", help="Use IACA markers", action='store_true')
   parser.add_argument("-raw", help="raw file", action='store_true')
   parser.add_argument("-arch", help="Microarchitecture", default='CFL')
   parser.add_argument("-trace", help="HTML trace", nargs='?', const='trace.html')
   parser.add_argument("-loop", help="loop", action='store_true')
   args = parser.parse_args()

   global arch, allPorts
   arch = args.arch
   allPorts = getAllPorts()

   if arch in ['HSW', 'BDW']:
      global macroFusibleInstrCanBeDecodedAsLastInstr
      macroFusibleInstrCanBeDecodedAsLastInstr = False
      global IQ_Width
      IQ_Width = 20
      global MITE_Width
      MITE_Width = 4
      global instrWithMoreThan2UopsDecodedAlone
      instrWithMoreThan2UopsDecodedAlone = True
      global pop5CRequiresComplexDecoder
      pop5CRequiresComplexDecoder = True

   instrDataDict = importlib.import_module('instrData.'+arch).instrData

   instructions = getInstructions(args.filename, args.raw, args.iacaMarkers, instrDataDict)
   lastApplicableInstr = [instr for instr in instructions if not instr.macroFusedWithPrevInstr][-1] # ignore macro-fused instr.
   adjustLatencies(instructions)
   computeUopProperties(instructions)
   #print instructions

   global clock
   clock = 0

   #uopGenerator = UopGenerator(instructions)
   retireQueue = deque()
   rb = ReorderBuffer(retireQueue)
   scheduler = Scheduler()

   frontEnd = FrontEnd(instructions, rb, scheduler, not args.loop)
   #   uopSource = Decoder(uopGenerator, IDQ)
   #else:
   #   uopSource = DSB(uopGenerator, IDQ)


   nRounds = 150
   uopsForRound = []


   done = False
   while True:
      frontEnd.cycle()
      while retireQueue:
         fusedUop = retireQueue.popleft()

         for uop in fusedUop.getUnfusedUops():
            instr = uop.prop.instr
            rnd = uop.rnd
            if rnd >= nRounds and clock > 500:
               done = True
               break
            if rnd >= len(uopsForRound):
               uopsForRound.append({instr: [] for instr in instructions})
            uopsForRound[rnd][instr].append(uop)

      if done:
         break

      clock += 1

   TP = None

   firstRelevantRound = 50
   lastRelevantRound = len(uopsForRound)-2 # last round may be incomplete, thus -2
   for rnd in range(lastRelevantRound, lastRelevantRound-5, -1):
      if uopsForRound[firstRelevantRound][lastApplicableInstr][-1].retireIdx == uopsForRound[rnd][lastApplicableInstr][-1].retireIdx:
         lastRelevantRound = rnd
         break

   uopsForRelRound = uopsForRound[firstRelevantRound:(lastRelevantRound+1)]

   TP = float(uopsForRelRound[-1][lastApplicableInstr][-1].retired - uopsForRelRound[0][lastApplicableInstr][-1].retired) / (len(uopsForRelRound)-1)
   #TP = max(float((uop2.retired-uop1.retired)) for d in uopsRoundDict.values() for (uop1, uop2) in zip(d[25], d[nRounds-25]))/(nRounds-50)

   print 'TP: {:.2f}'.format(TP)
   print ''

   printPortUsage(instructions, uopsForRelRound)

   if args.trace is not None:
      generateHTMLTraceTable(args.trace, instructions, uopsForRound, clock-1)

if __name__ == "__main__":
    main()

