#!/usr/bin/env python

import ntr, os, subprocess

# usage: put the js in a separate file from html.  give the js filename to lithium as --testcase and to this script as jsfile.
# for example:
# ./lithium.py --testcase=c.js shell_compiles_browser_dies.py 120 ~/central/debug-obj/dist/MinefieldDebug.app/Contents/MacOS/firefox-bin collecta.html

jsshell = os.path.expanduser("~/tracemonkey/js/src/debug/js")
jsfile = "c.js"

def interesting(args, tempPrefix):
    timeout = int(args[0])
    returncode = subprocess.call([jsshell, "-C", jsfile], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if returncode != 0:
        print "JS didn't compile, skipping browser test"
        return False
    runinfo = ntr.timed_run(args[1:], timeout, tempPrefix)
    print "Exit status: %s (%.3f seconds)" % (runinfo.msg, runinfo.elapsedtime)
    return runinfo.sta == ntr.CRASHED or runinfo.sta == ntr.ABNORMAL
