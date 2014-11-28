#!/usr/bin/env python
import getopt, sys, os, subprocess

# This is used for minimizing the number of strings.
import re, string

def usage():
    print """Lithium, an automated testcase reduction tool by Jesse Ruderman
    
Usage:
    
./lithium.py [options] condition [condition options] file-to-reduce

Example:

./lithium.py crashes 120 ~/tracemonkey/js/src/debug/js -j a.js
     Lithium will reduce a.js subject to the condition that the following 
     crashes in 120 seconds:
     ~/tracemonkey/js/src/debug/js -j a.js

Options:
* --char (-c).
      Don't treat lines as atomic units; treat the file as a sequence
      of characters rather than a sequence of lines.
* --strategy=[minimize, remove-pair, remove-substring, check-only].
      default: minimize.
* --testcase=filename.
      default: last thing on the command line, which can double as passing in.

Additional options for the default strategy (--strategy=minimize)
* --repeat=[always, last, never]. default: last
     Whether to repeat a chunk size if chunks are removed.
* --max=n. default: about half of the file.
* --min=n. default: 1.
     What chunk sizes to test.  Must be powers of two.
* --chunksize=n
     Shortcut for "repeat=never, min=n, max=n"

See doc/using.html for more information.

"""


# Globals

strategy = "minimize"
minimizeRepeat = "last"
minimizeMin = 1
minimizeMax = pow(2, 30)
    
atom = "line"
cutAfter = "?=;{["
cutBefore = "]}:"

conditionScript = None
conditionArgs = None
testcaseFilename = None
testcaseExtension = ""

testCount = 0
testTotal = 0

tempDir = None
tempFileCount = 1

before = ""
after = ""
parts = []


# Main and friends

def main():
    global conditionScript, conditionArgs, testcaseFilename, testcaseExtension, strategy
    global parts

    try:
        opts, args = getopt.getopt(sys.argv[1:], "hcs", [
            "help",
            "char", "symbols", "cutBefore=", "cutAfter=",
            "strategy=", "repeat=", "min=", "max=", "chunksize=",
            "testcase=", "tempdir="])
    except getopt.GetoptError, exc:
        usageError(exc.msg)

    if len(args) == 0:
        # No arguments; not even a condition was specified
        usage()
        sys.exit(0)

    if len(args) > 1:
        testcaseFilename = args[-1] # can be overridden by --testcase in processOptions
        
    processOptions(opts)

    if testcaseFilename == None:
        usageError("No testcase specified (use --testcase or last condition arg)")

    conditionScript = importRelativeOrAbsolute(args[0])
    conditionArgs = args[1:]

    if hasattr(conditionScript, "init"):
        conditionScript.init(conditionArgs)

    e = testcaseFilename.rsplit(".", 1)
    if len(e) > 1:
        testcaseExtension = "." + e[1]


    readTestcase()

    if tempDir == None:
        createTempDir()
        print "Intermediate files will be stored in " + tempDir + os.sep + "."

    if strategy == "check-only":
        print 'Interesting.' if interesting(parts) else 'Not interesting.'
        sys.exit(0)

    strategyFunction = {
        'minimize': minimize,
        'minimize-around': minimizeSurroundingPairs,
        'minimize-balanced': minimizeBalancedPairs,
        'replace-properties-by-globals': replacePropertiesByGlobals,
        'replace-arguments-by-globals': replaceArgumentsByGlobals,
        'remove-pair': tryRemovingPair,
        'remove-adjacent-pairs': tryRemovingAdjacentPairs,
        'remove-substring': tryRemovingSubstring
    }.get(strategy, None)

    if not strategyFunction:
        usageError("Unknown strategy!")

    print "The original testcase has " + quantity(len(parts), atom) + "."
    print "Checking that the original testcase is 'interesting'..."
    if not interesting(parts):
        usageError("The original testcase is not 'interesting'!")

    if len(parts) == 0:
        usageError("The file has " + quantity(0, atom) + " so there's nothing for Lithium to try to remove!")

    writeTestcaseTemp("original", False)
    strategyFunction()


def processOptions(opts):
    global atom, cutBefore, cutAfter, minimizeRepeat, minimizeMin, minimizeMax, strategy, testcaseFilename, tempDir

    for o, a in opts:
        if o in ("-h", "--help"):
            usage()
            sys.exit(0)
        elif o == "--testcase":
            testcaseFilename = a
        elif o == "--tempdir":
            tempDir = a
        elif o in ("-c", "--char"): 
            atom = "char"
        elif o in ("-s", "--symbols"):
            atom = "symbol-delimiter"
        elif o in ("--cut-after"):
            cutAfter = str(a)
        elif o in ("--cut-before"):
            cutBefore = str(a)
        elif o == "--strategy":
            strategy = a
        elif o == "--min":
            minimizeMin = int(a)
            if not isPowerOfTwo(minimizeMin):
                usageError("min must be a power of two.")
        elif o == "--max":
            minimizeMax = int(a)
            if not isPowerOfTwo(minimizeMax):
                usageError("max must be a power of two.")
        elif o == "--repeat":
            minimizeRepeat = a
            if not (minimizeRepeat in ("always", "last", "never")):
                usageError("repeat must be 'always', 'last', or 'never'.")
        elif o == "--chunksize":
            minimizeMin = int(a)
            minimizeMax = minimizeMin
            minimizeRepeat = "never"
            if not isPowerOfTwo(minimizeMin):
                usageError("Chunk size must be a power of two.")


def usageError(s):
    print s
    print "Use --help if you need it :)"
    sys.exit(2)


# Functions for manipulating the testcase (aka the 'interesting' file)

def readTestcase():    
    hasDDSection = False

    try:
        file = open(testcaseFilename, "r")
    except IOError:
        usageError("Can't read the original testcase file, " + testcaseFilename + "!")
    
    # Determine whether the file has a DDBEGIN..DDEND section.
    for line in file:
        if line.find("DDEND") != -1:
            usageError("The testcase (" + testcaseFilename + ") has a line containing 'DDEND' without a line containing 'DDBEGIN' before it.")
        if line.find("DDBEGIN") != -1:
            hasDDSection = True
            break

    file.seek(0)

    if hasDDSection:
        # Reduce only the part of the file between 'DDBEGIN' and 'DDEND',
        # leaving the rest unchanged.
        #print "Testcase has a DD section"
        readTestcaseWithDDSection(file)
    else:
        # Reduce the entire file.
        #print "Testcase does not have a DD section"
        for line in file:
            readTestcaseLine(line)
        
    file.close()


def readTestcaseWithDDSection(file):
    global before, after
    global parts

    for line in file:
        before += line
        if line.find("DDBEGIN") != -1:
            break

    for line in file:
        if line.find("DDEND") != -1:
            after += line
            break
        readTestcaseLine(line)
    else:
        usageError("The testcase (" + testcaseFilename + ") has a line containing 'DDBEGIN' but no line containing 'DDEND'.")

    for line in file:
        after += line
    
    if atom == "char" and len(parts) > 0:
        # Move the line break at the end of the last line out of the reducible
        # part so the "DDEND" line doesn't get combined with another line.
        parts.pop()
        after = "\n" + after


def readTestcaseLine(line):
    global atom
    global parts
    
    if atom == "line":
       parts.append(line)
    elif atom == "char":
        for char in line:
            parts.append(char)
    elif atom == "symbol-delimiter":
        cutter = '[' + cutBefore + ']?[^' + cutBefore + cutAfter + ']*(?:[' + cutAfter + ']|$|(?=[' + cutBefore + ']))'
        for statement in re.finditer(cutter, line):
            parts.append(statement.group(0))

def writeTestcase(filename):
    file = open(filename, "w")
    file.write(before)
    for i in range(len(parts)):
        file.write(parts[i])
    file.write(after)
    file.close()

def writeTestcaseTemp(partialFilename, useNumber):
    global tempFileCount
    if useNumber:
        partialFilename = str(tempFileCount) + "-" + partialFilename
        tempFileCount += 1
    writeTestcase(tempDir + os.sep + partialFilename + testcaseExtension)


def createTempDir():
    global tempDir
    i = 1
    while 1:
        tempDir = "tmp" + str(i)
        # To avoid race conditions, we use try/except instead of exists/create
        # Hopefully we don't get any errors other than "File exists" :)
        try:
            os.mkdir(tempDir)
            break
        except OSError, e:
            i += 1


# Interestingness test

def interesting(partsSuggestion):
    global tempFileCount, testcaseFilename, conditionArgs
    global testCount, testTotal
    global parts
    oldParts = parts # would rather be less side-effecty about this, and be passing partsSuggestion around
    parts = partsSuggestion

    writeTestcase(testcaseFilename)

    testCount += 1
    testTotal += len(parts)

    tempPrefix = tempDir + os.sep + str(tempFileCount)
    inter = conditionScript.interesting(conditionArgs, tempPrefix)

    # Save an extra copy of the file inside the temp directory.
    # This is useful if you're reducing an assertion and encounter a crash:
    # it gives you a way to try to reproduce the crash.
    if tempDir != None:
        tempFileTag = "interesting" if inter else "boring"
        writeTestcaseTemp(tempFileTag, True)

    if not inter:
        parts = oldParts
    return inter


# Main reduction algorithm

def minimize():
    origNumParts = len(parts)
    chunkSize = min(minimizeMax, largestPowerOfTwoSmallerThan(origNumParts))
    finalChunkSize = max(minimizeMin, 1)
    
    while 1:
        anyChunksRemoved = tryRemovingChunks(chunkSize);
    
        last = (chunkSize == finalChunkSize)

        if anyChunksRemoved and (minimizeRepeat == "always" or (minimizeRepeat == "last" and last)):
            # Repeat with the same chunk size
            pass
        elif last:
            # Done
            break
        else:
            # Continue with the next smaller chunk size
            chunkSize /= 2

    writeTestcase(testcaseFilename)
    
    print "Lithium is done!"

    if finalChunkSize == 1 and minimizeRepeat != "never":
        print "  Removing any single " + atom + " from the final file makes it uninteresting!"

    print "  Initial size: " + quantity(origNumParts, atom)
    print "  Final size: " + quantity(len(parts), atom)
    print "  Tests performed: " + str(testCount)
    print "  Test total: " + quantity(testTotal, atom)


def tryRemovingChunks(chunkSize):
    """Make a single run through the testcase, trying to remove chunks of size chunkSize.
    
    Returns True iff any chunks were removed."""
    
    global parts
    
    chunksSoFar = 0
    summary = ""

    chunksRemoved = 0
    chunksSurviving = 0
    atomsRemoved = 0
    atomsSurviving = 0

    print "Starting a round with chunks of " + quantity(chunkSize, atom) + "."

    
    numChunks = divideRoundingUp(len(parts), chunkSize)
    chunkStart = 0
    while chunkStart < len(parts):

        chunksSoFar += 1
        chunkEnd = min(len(parts), chunkStart + chunkSize)
        description = "chunk #" + str(chunksSoFar) + " of " + str(numChunks) + " chunks of size " + str(chunkSize)
        
        if interesting(parts[:chunkStart] + parts[chunkEnd:]):
            print "Yay, reduced it by removing " + description + " :)"
            chunksRemoved += 1
            atomsRemoved += (chunkEnd - chunkStart)
            summary += '-';
            # leave chunkStart the same
        else:
            print "Removing " + description + " made the file 'uninteresting'."
            chunksSurviving += 1
            atomsSurviving += (chunkEnd - chunkStart)
            summary += 'S';
            chunkStart += chunkSize

        # Put a space between each pair of chunks in the summary.
        # During 'minimize', this is useful because it shows visually which 
        # chunks used to be part of a single larger chunk.
        if chunksSoFar % 2 == 0:
            summary += " ";
  
    print ""
    print "Done with a round of chunk size " + str(chunkSize) + "!"
    print quantity(chunksSurviving, "chunk") + " survived; " + \
          quantity(chunksRemoved, "chunk") + " removed."
    print quantity(atomsSurviving, atom) + " survived; " + \
          quantity(atomsRemoved, atom) + " removed."
    print "Which chunks survived: " + summary
    print ""
    
    writeTestcaseTemp("did-round-" + str(chunkSize), True);
   
    return (chunksRemoved > 0)



#
# This Strategy attempt at removing pairs of chuncks which might be surrounding
# interesting code, but which cannot be removed independently of the other.
# This happens frequently with patterns such as:
#
#   if (cond) {
#      interesting();
#   }
#
# The value of the condition might not be interesting, but in order to reach the
# interesting code we still have to compute it, and keep extra code alive.
#
def minimizeSurroundingPairs():
    origNumParts = len(parts)
    chunkSize = min(minimizeMax, largestPowerOfTwoSmallerThan(origNumParts))
    finalChunkSize = max(minimizeMin, 1)

    while 1:
        anyChunksRemoved = tryRemovingSurroundingChunks(chunkSize);

        last = (chunkSize == finalChunkSize)

        if anyChunksRemoved and (minimizeRepeat == "always" or (minimizeRepeat == "last" and last)):
            # Repeat with the same chunk size
            pass
        elif last:
            # Done
            break
        else:
            # Continue with the next smaller chunk size
            chunkSize /= 2

    writeTestcase(testcaseFilename)
    
    print "Lithium is done!"

    if finalChunkSize == 1 and minimizeRepeat != "never":
        print "  Removing any single " + atom + " from the final file makes it uninteresting!"

    print "  Initial size: " + quantity(origNumParts, atom)
    print "  Final size: " + quantity(len(parts), atom)
    print "  Tests performed: " + str(testCount)
    print "  Test total: " + quantity(testTotal, atom)

def list_rindex(l, p, e):
    if p < 0 or p > len(l):
        raise ValueError("%s is not in list" % str(e))
    for index, item in enumerate(reversed(l[:p])):
        if item == e:
            return p - index - 1
    raise ValueError("%s is not in list" % str(e))

def list_nindex(l, p, e):
    if p + 1 >= len(l):
        raise ValueError("%s is not in list" % str(e))
    return l[(p + 1):].index(e) + (p + 1)

def tryRemovingSurroundingChunks(chunkSize):
    """Make a single run through the testcase, trying to remove chunks of size chunkSize.

    Returns True iff any chunks were removed."""

    global parts

    chunksSoFar = 0
    summary = ""

    chunksRemoved = 0
    chunksSurviving = 0
    atomsRemoved = 0

    atomsInitial = len(parts)
    numChunks = divideRoundingUp(len(parts), chunkSize)

    # Not enough chunks to remove surrounding blocks.
    if numChunks < 3:
        return False

    print "Starting a round with chunks of " + quantity(chunkSize, atom) + "."

    summary = ['S' for i in range(numChunks)]
    chunkStart = chunkSize
    beforeChunkIdx = 0
    keepChunkIdx = 1
    afterChunkIdx = 2

    try:
        while chunkStart + chunkSize < len(parts):
            chunkBefStart = max(0, chunkStart - chunkSize)
            chunkBefEnd = chunkStart
            chunkAftStart = min(len(parts), chunkStart + chunkSize)
            chunkAftEnd = min(len(parts), chunkAftStart + chunkSize)
            description = "chunk #" + str(beforeChunkIdx) + " & #" + str(afterChunkIdx) + " of " + str(numChunks) + " chunks of size " + str(chunkSize)

            if interesting(parts[:chunkBefStart] + parts[chunkBefEnd:chunkAftStart] + parts[chunkAftEnd:]):
                print "Yay, reduced it by removing " + description + " :)"
                chunksRemoved += 2
                atomsRemoved += (chunkBefEnd - chunkBefStart)
                atomsRemoved += (chunkAftEnd - chunkAftStart)
                summary[beforeChunkIdx] = '-'
                summary[afterChunkIdx] = '-'
                # The start is now sooner since we remove the chunk which was before this one.
                chunkStart -= chunkSize
                try:
                    # Try to keep removing surrounding chunks of the same part.
                    beforeChunkIdx = list_rindex(summary, keepChunkIdx, 'S')
                except ValueError:
                    # There is no more survinving block on the left-hand-side of
                    # the current chunk, shift everything by one surviving
                    # block. Any ValueError from here means that there is no
                    # longer enough chunk.
                    beforeChunkIdx = keepChunkIdx
                    keepChunkIdx = list_nindex(summary, keepChunkIdx, 'S')
                    chunkStart += chunkSize
            else:
                print "Removing " + description + " made the file 'uninteresting'."
                # Shift chunk indexes, and seek the next surviving chunk. ValueError
                # from here means that there is no longer enough chunks.
                beforeChunkIdx = keepChunkIdx
                keepChunkIdx = afterChunkIdx
                chunkStart += chunkSize

            afterChunkIdx = list_nindex(summary, keepChunkIdx, 'S')

    except ValueError:
        # This is a valid loop exit point.
        chunkStart = len(parts)

    atomsSurviving = atomsInitial - atomsRemoved
    printableSummary = " ".join(["".join(summary[(2 * i):min(2 * (i + 1), numChunks + 1)]) for i in range(numChunks / 2 + numChunks % 2)])
    print ""
    print "Done with a round of chunk size " + str(chunkSize) + "!"
    print quantity(summary.count('S'), "chunk") + " survived; " + \
          quantity(summary.count('-'), "chunk") + " removed."
    print quantity(atomsSurviving, atom) + " survived; " + \
          quantity(atomsRemoved, atom) + " removed."
    print "Which chunks survived: " + printableSummary
    print ""

    writeTestcaseTemp("did-round-" + str(chunkSize), True);

    return (chunksRemoved > 0)


#
# This Strategy attempt at removing balanced chuncks which might be surrounding
# interesting code, but which cannot be removed independently of the other.
# This happens frequently with patterns such as:
#
#   if (cond) {
#      ...;
#      ...;
#      interesting();
#      ...;
#      ...;
#   }
#
# The value of the condition might not be interesting, but in order to reach the
# interesting code we still have to compute it, and keep extra code alive.
#
def minimizeBalancedPairs():
    origNumParts = len(parts)
    chunkSize = min(minimizeMax, largestPowerOfTwoSmallerThan(origNumParts))
    finalChunkSize = max(minimizeMin, 1)

    while 1:
        anyChunksRemoved = tryRemovingBalancedPairs(chunkSize);

        last = (chunkSize == finalChunkSize)

        if anyChunksRemoved and (minimizeRepeat == "always" or (minimizeRepeat == "last" and last)):
            # Repeat with the same chunk size
            pass
        elif last:
            # Done
            break
        else:
            # Continue with the next smaller chunk size
            chunkSize /= 2

    writeTestcase(testcaseFilename)

    print "Lithium is done!"

    if finalChunkSize == 1 and minimizeRepeat != "never":
        print "  Removing any single " + atom + " from the final file makes it uninteresting!"

    print "  Initial size: " + quantity(origNumParts, atom)
    print "  Final size: " + quantity(len(parts), atom)
    print "  Tests performed: " + str(testCount)
    print "  Test total: " + quantity(testTotal, atom)

def list_fiveParts(list, step, f, s, t):
    return (list[:f], list[f:s], list[s:(s+step)], list[(s+step):(t+step)], list[(t+step):])

def tryRemovingBalancedPairs(chunkSize):
    """Make a single run through the testcase, trying to remove chunks of size chunkSize.

    Returns True iff any chunks were removed."""

    global parts

    chunksSoFar = 0
    summary = ""

    chunksRemoved = 0
    chunksSurviving = 0
    atomsRemoved = 0

    atomsInitial = len(parts)
    numChunks = divideRoundingUp(len(parts), chunkSize)

    # Not enough chunks to remove surrounding blocks.
    if numChunks < 2:
        return False

    print "Starting a round with chunks of " + quantity(chunkSize, atom) + "."

    summary = ['S' for i in range(numChunks)]
    curly = [(parts[i].count('{') - parts[i].count('}')) for i in range(numChunks)]
    square = [(parts[i].count('{') - parts[i].count('}')) for i in range(numChunks)]
    normal = [(parts[i].count('(') - parts[i].count(')')) for i in range(numChunks)]
    chunkStart = 0
    lhsChunkIdx = 0

    try:
        while chunkStart < len(parts):

            description = "chunk #" + str(lhsChunkIdx) + "".join([" " for i in range(len(str(lhsChunkIdx)) + 4)])
            description += " of " + str(numChunks) + " chunks of size " + str(chunkSize)

            assert summary[:lhsChunkIdx].count('S') * chunkSize == chunkStart, "the chunkStart should correspond to the lhsChunkIdx modulo the removed chunks."

            chunkLhsStart = chunkStart
            chunkLhsEnd = min(len(parts), chunkLhsStart + chunkSize)

            nCurly = curly[lhsChunkIdx]
            nSquare = square[lhsChunkIdx]
            nNormal = normal[lhsChunkIdx]

            # If the chunk is already balanced, try to remove it.
            if nCurly == 0 and nSquare == 0 and nNormal == 0:
                if interesting(parts[:chunkLhsStart] + parts[chunkLhsEnd:]):
                    print "Yay, reduced it by removing " + description + " :)"
                    chunksRemoved += 1
                    atomsRemoved += (chunkLhsEnd - chunkLhsStart)
                    summary[lhsChunkIdx] = '-'
                else:
                    print "Removing " + description + " made the file 'uninteresting'."
                    chunkStart += chunkSize
                lhsChunkIdx = list_nindex(summary, lhsChunkIdx, 'S')
                continue

            # Otherwise look for the corresponding chunk.
            rhsChunkIdx = lhsChunkIdx
            for item in summary[(lhsChunkIdx + 1):]:
                rhsChunkIdx += 1
                if item != 'S':
                    continue
                nCurly += curly[rhsChunkIdx]
                nSquare += square[rhsChunkIdx]
                nNormal += normal[rhsChunkIdx]
                if nCurly < 0 or nSquare < 0 or nNormal < 0:
                    break
                if nCurly == 0 and nSquare == 0 and nNormal == 0:
                    break

            # If we have no match, then just skip this pair of chunks.
            if nCurly != 0 or nSquare != 0 or nNormal != 0:
                print "Skipping " + description + " because it is 'uninteresting'."
                chunkStart += chunkSize
                lhsChunkIdx = list_nindex(summary, lhsChunkIdx, 'S')
                continue

            # Otherwise we do have a match and we check if this is interesting to remove both.
            chunkRhsStart = chunkLhsStart + chunkSize * summary[lhsChunkIdx:rhsChunkIdx].count('S')
            chunkRhsStart = min(len(parts), chunkRhsStart)
            chunkRhsEnd = min(len(parts), chunkRhsStart + chunkSize)

            description = "chunk #" + str(lhsChunkIdx) + " & #" + str(rhsChunkIdx)
            description += " of " + str(numChunks) + " chunks of size " + str(chunkSize)

            if interesting(parts[:chunkLhsStart] + parts[chunkLhsEnd:chunkRhsStart] + parts[chunkRhsEnd:]):
                print "Yay, reduced it by removing " + description + " :)"
                chunksRemoved += 2
                atomsRemoved += (chunkLhsEnd - chunkLhsStart)
                atomsRemoved += (chunkRhsEnd - chunkRhsStart)
                summary[lhsChunkIdx] = '-'
                summary[rhsChunkIdx] = '-'
                lhsChunkIdx = list_nindex(summary, lhsChunkIdx, 'S')
                continue

            # Removing the braces make the failure disappear.  As we are looking
            # for removing chunk (braces), we need to make the content within
            # the braces as minimal as possible, so let us try to see if we can
            # move the chunks outside the braces.
            print "Removing " + description + " made the file 'uninteresting'."

            # Moving chunks is still a bit experimental, and it can introduce reducing loops.
            # If you want to try it, just replace this True by a False.
            if True:
                chunkStart += chunkSize
                lhsChunkIdx = list_nindex(summary, lhsChunkIdx, 'S')
                continue

            origChunkIdx = lhsChunkIdx
            stayOnSameChunk = False
            chunkMidStart = chunkLhsEnd
            midChunkIdx = list_nindex(summary, lhsChunkIdx, 'S')
            while chunkMidStart < chunkRhsStart:
                assert summary[:midChunkIdx].count('S') * chunkSize == chunkMidStart, "the chunkMidStart should correspond to the midChunkIdx modulo the removed chunks."
                description = "chunk #" + str(midChunkIdx) + "".join([" " for i in range(len(str(lhsChunkIdx)) + 4)])
                description += " of " + str(numChunks) + " chunks of size " + str(chunkSize)

                chunkMidEnd = chunkMidStart + chunkSize
                p = list_fiveParts(parts, chunkSize, chunkLhsStart, chunkMidStart, chunkRhsStart)

                nCurly = curly[midChunkIdx]
                nSquare = square[midChunkIdx]
                nNormal = normal[midChunkIdx]
                if nCurly != 0 or nSquare != 0 or nNormal != 0:
                    print "Keepping " + description + " because it is 'uninteresting'."
                    chunkMidStart += chunkSize
                    midChunkIdx = list_nindex(summary, midChunkIdx, 'S')
                    continue

                # Try moving the chunk after.
                if interesting(p[0] + p[1] + p[3] + p[2] + p[4]):
                    print "->Moving " + description + " kept the file 'interesting'."
                    chunkRhsStart -= chunkSize
                    chunkRhsEnd -= chunkSize
                    tS = list_fiveParts(summary, 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    tc = list_fiveParts(curly  , 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    ts = list_fiveParts(square , 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    tn = list_fiveParts(normal , 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    summary = tS[0] + tS[1] + tS[3] + tS[2] + tS[4]
                    curly =   tc[0] + tc[1] + tc[3] + tc[2] + tc[4]
                    square =  ts[0] + ts[1] + ts[3] + ts[2] + ts[4]
                    normal =  tn[0] + tn[1] + tn[3] + tn[2] + tn[4]
                    rhsChunkIdx -= 1
                    midChunkIdx = summary[midChunkIdx:].index('S') + midChunkIdx
                    continue

                # Try moving the chunk before.
                if interesting(p[0] + p[2] + p[1] + p[3] + p[4]):
                    print "<-Moving " + description + " kept the file 'interesting'."
                    chunkLhsStart += chunkSize
                    chunkLhsEnd += chunkSize
                    chunkMidStart += chunkSize
                    tS = list_fiveParts(summary, 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    tc = list_fiveParts(curly  , 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    ts = list_fiveParts(square , 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    tn = list_fiveParts(normal , 1, lhsChunkIdx, midChunkIdx, rhsChunkIdx)
                    summary = tS[0] + tS[2] + tS[1] + tS[3] + tS[4]
                    curly =   tc[0] + tc[2] + tc[1] + tc[3] + tc[4]
                    square =  ts[0] + ts[2] + ts[1] + ts[3] + ts[4]
                    normal =  tn[0] + tn[2] + tn[1] + tn[3] + tn[4]
                    lhsChunkIdx += 1
                    midChunkIdx = list_nindex(summary, midChunkIdx, 'S')
                    stayOnSameChunk = True
                    continue

                print "..Moving " + description + " made the file 'uninteresting'."
                chunkMidStart += chunkSize
                midChunkIdx = list_nindex(summary, midChunkIdx, 'S')

            lhsChunkIdx = origChunkIdx
            if not stayOnSameChunk:
                chunkStart += chunkSize
                lhsChunkIdx = list_nindex(summary, lhsChunkIdx, 'S')


    except ValueError:
        # This is a valid loop exit point.
        chunkStart = len(parts)

    atomsSurviving = atomsInitial - atomsRemoved
    printableSummary = " ".join(["".join(summary[(2 * i):min(2 * (i + 1), numChunks + 1)]) for i in range(numChunks / 2 + numChunks % 2)])
    print ""
    print "Done with a round of chunk size " + str(chunkSize) + "!"
    print quantity(summary.count('S'), "chunk") + " survived; " + \
          quantity(summary.count('-'), "chunk") + " removed."
    print quantity(atomsSurviving, atom) + " survived; " + \
          quantity(atomsRemoved, atom) + " removed."
    print "Which chunks survived: " + printableSummary
    print ""

    writeTestcaseTemp("did-round-" + str(chunkSize), True);

    return (chunksRemoved > 0)



#
# This Strategy attempt at removing members, such as other strategies can then
# move the lines out-side the functions.  The goal is to rename variable at the
# same time, such as the program remain valid, while removing the dependency on
# the object on which the member is.
#
#   function Foo() {
#     this.list = [];
#   }
#   Foo.prototype.push = function(a) {
#     this.list.push(a);
#   }
#   Foo.prototype.last = function() {
#     return this.list.pop();
#   }
#
def replacePropertiesByGlobals():
    origNumParts = len(parts)
    chunkSize = min(minimizeMax, 2 * largestPowerOfTwoSmallerThan(origNumParts))
    finalChunkSize = max(minimizeMin, 1)

    origNumChars = 0
    for line in parts:
        origNumChars += len(line)

    numChars = origNumChars
    while 1:
        numRemovedChars = tryMakingGlobals(chunkSize, numChars);
        numChars -= numRemovedChars

        last = (chunkSize == finalChunkSize)

        if numRemovedChars and (minimizeRepeat == "always" or (minimizeRepeat == "last" and last)):
            # Repeat with the same chunk size
            pass
        elif last:
            # Done
            break
        else:
            # Continue with the next smaller chunk size
            chunkSize /= 2

    writeTestcase(testcaseFilename)

    print "Lithium is done!"

    if finalChunkSize == 1 and minimizeRepeat != "never":
        print "  Removing any single " + atom + " from the final file makes it uninteresting!"

    print "  Initial size: " + quantity(origNumChars, "character")
    print "  Final size: " + quantity(numChars, "character")
    print "  Tests performed: " + str(testCount)
    print "  Test total: " + quantity(testTotal, atom)


def tryMakingGlobals(chunkSize, numChars):
    """Make a single run through the testcase, trying to remove chunks of size chunkSize.

    Returns True iff any chunks were removed."""

    global parts

    summary = ""

    numRemovedChars = 0
    numChunks = divideRoundingUp(len(parts), chunkSize)
    finalChunkSize = max(minimizeMin, 1)

    # Map words to the chunk indexes in which they are present.
    words = {}
    for chunk, line in enumerate(parts):
        for match in re.finditer(r'(?<=[\w\d_])\.(\w+)', line):
            word = match.group(1)
            if not word in words:
                words[word] = [chunk]
            else:
                words[word] += [chunk]

    # All patterns have been removed sucessfully.
    if len(words) == 0:
        return 0

    print "Starting a round with chunks of " + quantity(chunkSize, atom) + "."
    summary = ['S' for i in range(numChunks)]

    for word, chunks in words.items():
        chunkIndexes = {}
        for chunkStart in chunks:
            chunkIdx = int(chunkStart / chunkSize)
            if not chunkIdx in chunkIndexes:
                chunkIndexes[chunkIdx] = [chunkStart]
            else:
                chunkIndexes[chunkIdx] += [chunkStart]

        for chunkIdx, chunkStarts in chunkIndexes.items():
            # Unless this is the final size, let's try to remove couple of
            # prefixes, otherwise wait for the final size to remove each of them
            # individually.
            if len(chunkStarts) == 1 and finalChunkSize != chunkSize:
                continue

            description = "'" + word + "' in "
            description += "chunk #" + str(chunkIdx) + " of " + str(numChunks) + " chunks of size " + str(chunkSize)

            maybeRemoved = 0
            newParts = parts
            for chunkStart in chunkStarts:
                subst = re.sub("[\w_.]+\." + word, word, newParts[chunkStart])
                maybeRemoved += len(newParts[chunkStart]) - len(subst)
                newParts = newParts[:chunkStart] + [ subst ] + newParts[(chunkStart+1):]

            if interesting(newParts):
                print "Yay, reduced it by removing prefixes of " + description + " :)"
                numRemovedChars += maybeRemoved
                summary[chunkIdx] = 's'
                words[word] = [ c for c in chunks if c not in chunkIndexes ]
                if len(words[word]) == 0:
                    del words[word]
            else:
                print "Removing prefixes of " + description + " made the file 'uninteresting'."

    numSurvivingChars = numChars - numRemovedChars
    printableSummary = " ".join(["".join(summary[(2 * i):min(2 * (i + 1), numChunks + 1)]) for i in range(numChunks / 2 + numChunks % 2)])
    print ""
    print "Done with a round of chunk size " + str(chunkSize) + "!"
    print quantity(summary.count('S'), "chunk") + " survived; " + \
          quantity(summary.count('s'), "chunk") + " shortened."
    print quantity(numSurvivingChars, "character") + " survived; " + \
          quantity(numRemovedChars, "character") + " removed."
    print "Which chunks survived: " + printableSummary
    print ""

    writeTestcaseTemp("did-round-" + str(chunkSize), True);

    return numRemovedChars


#
# This Strategy attempt at replacing arguments by globals, for each named
# argument of a function we add a setter of the global of the same name before
# the function call.  The goal is to remove functions by making empty arguments
# lists instead.
#
#   function foo(a,b) {
#     list = a + b;
#   }
#   foo(2, 3)
#
# becomes:
#
#   function foo() {
#     list = a + b;
#   }
#   a = 2;
#   b = 3;
#   foo()
#
# The next logical step is inlining the body of the function at the call-site.
#
def replaceArgumentsByGlobals():
    roundNum = 0
    while 1:
        numRemovedArguments = tryArgumentsAsGlobals(roundNum)
        roundNum += 1

        if numRemovedArguments and (minimizeRepeat == "always" or minimizeRepeat == "last"):
            # Repeat with the same chunk size
            pass
        else:
            # Done
            break

    writeTestcase(testcaseFilename)

    print "Lithium is done!"
    print "  Tests performed: " + str(testCount)
    print "  Test total: " + quantity(testTotal, atom)


def tryArgumentsAsGlobals(roundNum):
    """Make a single run through the testcase, trying to remove chunks of size chunkSize.
    
    Returns True iff any chunks were removed."""

    global parts

    numMovedArguments = 0
    numSurvivedArguments = 0

    # Map words to the chunk indexes in which they are present.
    functions = {}
    anonymousQueue = []
    anonymousStack = []
    for chunk, line in enumerate(parts):
        # Match function definition with at least one argument.
        for match in re.finditer(r'(?:function\s+(\w+)|(\w+)\s*=\s*function)\s*\((\s*\w+\s*(?:,\s*\w+\s*)*)\)', line):
            fun = match.group(1)
            if fun is None:
                fun = match.group(2)

            if match.group(3) == "":
                args = []
            else:
                args = match.group(3).split(',')

            if not fun in functions:
                functions[fun] = { "defs": args, "argsPattern": match.group(3), "chunk": chunk, "uses": [] }
            else:
                functions[fun]["defs"] = args
                functions[fun]["argsPattern"] = match.group(3)
                functions[fun]["chunk"] = chunk


        # Match anonymous function definition, which are surrounded by parentheses.
        for match in re.finditer(r'\(function\s*\w*\s*\(((?:\s*\w+\s*(?:,\s*\w+\s*)*)?)\)\s*{', line):
            if match.group(1) == "":
                args = []
            else:
                args = match.group(1).split(',')
            anonymousStack += [{ "defs": args, "chunk": chunk, "use": None, "useChunk": 0 }]

        # Match calls of anonymous function.
        for match in re.finditer(r'}\s*\)\s*\(((?:[^()]|\([^,()]*\))*)\)', line):
            if len(anonymousStack) == 0:
                continue
            anon = anonymousStack[-1]
            anonymousStack = anonymousStack[:-1]
            if match.group(1) == "" and len(anon["defs"]) == 0:
                continue
            if match.group(1) == "":
                args = []
            else:
                args = match.group(1).split(',')
            anon["use"] = args
            anon["useChunk"] = chunk
            anonymousQueue += [anon]

        # match function calls. (and some definitions)
        for match in re.finditer(r'((\w+)\s*\(((?:[^()]|\([^,()]*\))*)\))', line):
            pattern = match.group(1)
            fun = match.group(2)
            if match.group(3) == "":
                args = []
            else:
                args = match.group(3).split(',')
            if not fun in functions:
                functions[fun] = { "uses": [] }
            functions[fun]["uses"] += [{ "values": args, "chunk": chunk, "pattern": pattern }]


    # All patterns have been removed sucessfully.
    if len(functions) == 0 and len(anonymousQueue) == 0:
        return 0

    print "Starting removing function arguments."

    for fun, argsMap in functions.items():
        description = "arguments of '" + fun + "'"
        if "defs" not in argsMap or len(argsMap["uses"]) == 0:
            print "Ignoring " + description + " because it is 'uninteresting'."
            continue

        maybeMovedArguments = 0
        newParts = parts

        # Remove the function definition arguments
        argDefs = argsMap["defs"]
        defChunk = argsMap["chunk"]
        subst = string.replace(newParts[defChunk], argsMap["argsPattern"], "", 1)
        newParts = newParts[:defChunk] + [ subst ] + newParts[(defChunk+1):]

        # Copy callers arguments to globals.
        for argUse in argsMap["uses"]:
            values = argUse["values"]
            chunk = argUse["chunk"]
            if chunk == defChunk and values == argDefs:
                continue
            while len(values) < len(argDefs):
                values = values + ["undefined"]
            setters = "".join([ a + " = " + v + ";\n" for a, v in zip(argDefs, values) ])
            subst = setters + newParts[chunk]
            newParts = newParts[:chunk] + [ subst ] + newParts[(chunk+1):]
            maybeMovedArguments += len(values);

        if interesting(newParts):
            print "Yay, reduced it by removing " + description + " :)"
            numMovedArguments += maybeMovedArguments
        else:
            numSurvivedArguments += maybeMovedArguments
            print "Removing " + description + " made the file 'uninteresting'."

        for argUse in argsMap["uses"]:
            chunk = argUse["chunk"]
            values = argUse["values"]
            if chunk == defChunk and values == argDefs:
                continue

            newParts = parts
            subst = string.replace(newParts[chunk], argUse["pattern"], fun + "()", 1)
            if newParts[chunk] == subst:
                continue
            newParts = newParts[:chunk] + [ subst ] + newParts[(chunk+1):]
            maybeMovedArguments = len(values);

            descriptionChunk = description + " at " + atom + " #" + str(chunk)
            if interesting(newParts):
                print "Yay, reduced it by removing " + descriptionChunk + " :)"
                numMovedArguments += maybeMovedArguments
            else:
                numSurvivedArguments += maybeMovedArguments
                print "Removing " + descriptionChunk + " made the file 'uninteresting'."

    # Remove immediate anonymous function calls.
    for anon in anonymousQueue:
        noopChanges = 0
        maybeMovedArguments = 0
        newParts = parts

        argDefs = anon["defs"]
        defChunk = anon["chunk"]
        values = anon["use"]
        chunk = anon["useChunk"]
        description = "arguments of anonymous function at #" + atom + " " + str(defChunk)

        # Remove arguments of the function.
        subst = string.replace(newParts[defChunk], ",".join(argDefs), "", 1)
        if newParts[defChunk] == subst:
            noopChanges += 1
        newParts = newParts[:defChunk] + [ subst ] + newParts[(defChunk+1):]

        # Replace arguments by their value in the scope of the function.
        while len(values) < len(argDefs):
            values = values + ["undefined"]
        setters = "".join([ "var " + a + " = " + v + ";\n" for a, v in zip(argDefs, values) ])
        subst = newParts[defChunk] + "\n" + setters
        if newParts[defChunk] == subst:
            noopChanges += 1
        newParts = newParts[:defChunk] + [ subst ] + newParts[(defChunk+1):]

        # Remove arguments of the anonymous function call.
        subst = string.replace(newParts[chunk], ",".join(anon["use"]), "", 1)
        if newParts[chunk] == subst:
            noopChanges += 1
        newParts = newParts[:chunk] + [ subst ] + newParts[(chunk+1):]
        maybeMovedArguments += len(values);

        if noopChanges == 3:
            continue

        if interesting(newParts):
            print "Yay, reduced it by removing " + description + " :)"
            numMovedArguments += maybeMovedArguments
        else:
            numSurvivedArguments += maybeMovedArguments
            print "Removing " + description + " made the file 'uninteresting'."


    print ""
    print "Done with this round!"
    print quantity(numMovedArguments, "argument") + " moved;"
    print quantity(numSurvivedArguments, "argument") + " survived."

    writeTestcaseTemp("did-round-" + str(roundNum), True);

    return numMovedArguments


# Other reduction algorithms
# (Use these if you're really frustrated with something you know is 1-minimal.)


def tryRemovingAdjacentPairs():
    # XXX capture the idea that after removing (4,5) it might be sensible to remove (3,6)
    # but also that after removing (2,3) and (4,5) it might be sensible to remove (1,6)
    # XXX also want to remove three at a time, and two at a time that are one line apart
    for i in range(0, numParts - 2):
        if enabled[i]:
            enabled[i] = False
            enabled[i + 1] = False
            if interesting():
                print "Removed an adjacent pair based at " + str(i)
            else:
                enabled[i] = True
                enabled[i + 1] = True
    # Restore the original testcase
    writeTestcase(testcaseFilename)
    print "Done with one pass of removing adjacent pairs"



def tryRemovingPair():
    for i in range(0, numParts):
        enabled[i] = False
        for j in range(i + 1, numParts):
            enabled[j] = False
            print "Trying removing the pair " + str(i) + ", " + str(j)
            if interesting():
                print "Success!  Removed a pair!  Exiting."
                sys.exit(0)
            enabled[j] = True
        enabled[i] = True

    # Restore the original testcase
    writeTestcase(testcaseFilename)
    print "Failure!  No pair can be removed."
            

def tryRemovingSubstring():
    for i in range(0, numParts):
        for j in range(i, numParts):
            enabled[j] = False
            print "Trying removing the substring " + str(i) + ".." + str(j)
            if interesting():
                print "Success!  Removed a substring!  Exiting."
                sys.exit(0)
        for j in range(i, numParts):
            enabled[j] = True

    # Restore the original testcase
    writeTestcase(testcaseFilename)
    print "Failure!  No substring can be removed."
    

# Helpers

def divideRoundingUp(n, d):
    return (n // d) + (1 if n % d != 0 else 0)

def isPowerOfTwo(n):
    i = 1
    while 1:
        if i == n:
            return True
        if i > n:
            return False
        i *= 2
    
def largestPowerOfTwoSmallerThan(n):
    i = 1
    while 1:
        if i * 2 >= n:
            return i
        i *= 2

def quantity(n, s):
    """Convert a quantity to a string, with correct pluralization."""
    r = str(n) + " " + s
    if n != 1:
        r += "s"
    return r

def importRelativeOrAbsolute(f):
    # maybe there's a way to do this more sanely with the |imp| module...
    if f.endswith(".py"):
        f = f[:-3]
    if f.rfind(os.path.sep):
        # Add the path part of the filename to the import path
        (p, _, f) = f.rpartition(os.path.sep)
        sys.path.append(p)
    else:
        # Add working directory to the import path
        sys.path.append(".")
    module = __import__(f)
    del sys.path[0]
    return module

# Run main

if __name__ == "__main__":
    main()
