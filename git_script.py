import subprocess
import sys

def run(cmd):
    p = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    out, err = p.communicate()
    print("STDOUT:", out.decode('utf-8'))
    print("STDERR:", err.decode('utf-8'))

run("git status -s")
run("git log -n 3 --oneline")
