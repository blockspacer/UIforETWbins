﻿# Copyright 2015 Google Inc. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
This script exists to work around severe performance problems when WPA or other
Windows Performance Toolkit programs try to load the symbols for the Chrome
web browser. Some combination of the enormous size of the symbols or the
enhanced debug information generated by /Zo causes WPA to take about twenty
minutes to process the symbols for chrome.dll and chrome_child.dll. When
profiling Chrome this delay happens with every new set of symbols, so with
every new version of Chrome.

This script uses xperf actions to dump a list of the symbols referenced in
an ETW trace. If chrome.dll, chrome_child.dll, content.dll, or blink_web.dll are
detected and if decoded symbols are not found in %_NT_SYMCACHE_PATH% (default is
c:\symcache) then RetrieveSymbols.exe is used to download the symbols from the
Chromium symbol server, pdbcopy.exe is used to strip the private symbols, and
then another xperf action is used to load the stripped symbols, thus converting
them to .symcache files that can be efficiently loaded by WPA.

Locally built Chrome symbols are also supported.

More details on the discovery of this slowness and the evolution of the fix
can be found here:
https://randomascii.wordpress.com/2014/11/04/slow-symbol-loading-in-microsofts-profiler-take-two/

Discussion can be found here:
https://randomascii.wordpress.com/2013/03/09/symbols-the-microsoft-way/

Source code for RetrieveSymbols.exe can be found here:
https://github.com/google/UIforETW/tree/master/RetrieveSymbols

If "chromium-browser-symsrv" is not found in _NT_SYMBOL_PATH or RetrieveSymbols.exe
and pdbcopy.exe are not found then this script will exit early.

With the 10.0.14393 version of WPA the symbol translation problems have largely
been eliminated, which seems like it would make this script unnecessary, but the
symbol translation slowdowns have been replaced by a bug in downloading symbols from
Chrome's symbol server.
"""
from __future__ import print_function

import os
import sys
import re
import tempfile
import shutil
import subprocess

# Set to true to do symbol translation as well as downloading. Set to
# false to just download symbols and let WPA translate them.
strip_and_translate = True

def main():
  if len(sys.argv) < 2:
    print("Usage: %s trace.etl" % sys.argv[0])
    sys.exit(0)

  # Our usage of subprocess seems to require Python 2.7+
  if sys.version_info.major == 2 and sys.version_info.minor < 7:
    print("Your python version is too old - 2.7 or higher required.")
    print("Python version is %s" % sys.version)
    sys.exit(0)

  symbol_path = os.environ.get("_NT_SYMBOL_PATH", "")
  if symbol_path.count("chromium-browser-symsrv") == 0:
    print("Chromium symbol server is not in _NT_SYMBOL_PATH. No symbol stripping needed.")
    sys.exit(0)

  script_dir = os.path.split(sys.argv[0])[0]
  retrieve_path = os.path.join(script_dir, "RetrieveSymbols.exe")
  pdbcopy_path = os.path.join(script_dir, "pdbcopy.exe")
  if os.environ.has_key("programfiles(x86)"):
    # The UIforETW copy of pdbcopy.exe fails to copy some Chrome PDBs that the
    # Windows 10 SDK version can copy - use it if present.
    pdbcopy_install = os.path.join(os.environ["programfiles(x86)"], r"Windows kits\10\debuggers\x86\pdbcopy.exe")
    if os.path.exists(pdbcopy_install):
      pdbcopy_path = pdbcopy_install

  # This tool converts PDBs created with /debug:fastlink (VC++ 2015 feature) to
  # regular PDBs that contain all of the symbol information directly. This is
  # required so that pdbcopy can copy the symbols.
  un_fastlink_tool = r"C:\Program Files (x86)\Microsoft Visual Studio 14.0\VC\bin\amd64\mspdbcmf.exe"
  if not os.path.exists(un_fastlink_tool):
    un_fastlink_tool = None

  # RetrieveSymbols.exe requires some support files. dbghelp.dll and symsrv.dll
  # have to be in the same directory as RetrieveSymbols.exe and pdbcopy.exe must
  # be in the path, so copy them all to the script directory.
  for third_party in ["pdbcopy.exe", "dbghelp.dll", "symsrv.dll"]:
    if not os.path.exists(third_party):
      source = os.path.normpath(os.path.join(script_dir, r"..\third_party", \
          third_party))
      dest = os.path.normpath(os.path.join(script_dir, third_party))
      shutil.copy2(source, dest)

  if not os.path.exists(pdbcopy_path):
    print("pdbcopy.exe not found. No symbol stripping is possible.")
    sys.exit(0)

  if not os.path.exists(retrieve_path):
    print("RetrieveSymbols.exe not found. No symbol retrieval is possible.")
    sys.exit(0)

  tracename = sys.argv[1]
  # Each symbol file that we pdbcopy gets copied to a separate directory so
  # that we can support decoding symbols for multiple chrome versions without
  # filename collisions.
  tempdirs = []

  # Typical output looks like:
  # "[RSDS] PdbSig: {0e7712be-af06-4421-884b-496f833c8ec1}; Age: 33; Pdb: D:\src\chromium2\src\out\Release\initial\chrome.dll.pdb"
  # Note that this output implies a .symcache filename like this:
  # chrome.dll-0e7712beaf064421884b496f833c8ec121v2.symcache
  # In particular, note that the xperf action prints the age in decimal, but the
  # symcache names use the age in hexadecimal!
  pdb_re = re.compile(r'"\[RSDS\] PdbSig: {(.*-.*-.*-.*-.*)}; Age: (.*); Pdb: (.*)"')
  pdb_cached_re = re.compile(r"Found .*file - placed it in (.*)")

  print("Pre-translating chrome symbols from stripped PDBs to avoid 10-15 minute translation times "
        "and to work around WPA symbol download bugs.")

  symcache_files = []
  # Keep track of the local symbol files so that we can temporarily rename them
  # to stop xperf from using -- rename them from .pdb to .pdbx
  local_symbol_files = []

  #-tle = tolerate lost events
  #-tti = tolerate time ivnersions
  #-a symcache = show image and symbol identification (see xperf -help processing)
  #-dbgid = show symbol identification information (see xperf -help symcache)
  command = 'xperf -i "%s" -tle -tti -a symcache -dbgid' % tracename
  print("> %s" % command)
  found_uncached = False
  raw_command_output = subprocess.check_output(command, stderr=subprocess.STDOUT)
  command_output = str(raw_command_output).splitlines()

  for line in command_output:
    dllMatch = None # This is the name to use when generating the .symcache files
    if line.count("chrome_child.dll") > 0:
      # The symcache files for chrome_child.dll use the name chrome.dll for some reason
      dllMatch = "chrome.dll"
    # Complete list of Chrome executables and binaries. Some are only used in internal builds.
    # Note that case matters for downloading PDBs.
    for dllName in ["chrome.exe", "chrome.dll", "blink_web.dll", "content.dll", "chrome_elf.dll", "chrome_watcher.dll", "libEGL.dll", "libGLESv2.dll"]:
      if line.count("\\" + dllName) > 0:
        dllMatch = dllName
    if dllMatch:
      match = pdb_re.match(line)
      if match:
        guid, age, path = match.groups()
        guid = guid.replace("-", "")
        age = int(age) # Prepare for printing as hex
        filepart = os.path.split(path)[1]
        symcache_file = r"c:\symcache\%s-%s%xv2.symcache" % (dllMatch, guid, age)
        if os.path.exists(symcache_file):
          #print("Symcache file %s already exists. Skipping." % symcache_file)
          continue
        # Only print messages for chrome PDBs that aren't in the symcache
        found_uncached = True
        print("Found uncached reference to %s: %s - %s" % (filepart, guid, age, ))
        symcache_files.append(symcache_file)
        pdb_cache_path = None
        retrieve_command = "%s %s %s %s" % (retrieve_path, guid, age, filepart)
        print("  > %s" % retrieve_command)
        for subline in os.popen(retrieve_command):
          cache_match = pdb_cached_re.match(subline.strip())
          if cache_match:
            pdb_cache_path = cache_match.groups()[0]
            # RetrieveSymbols puts a period at the end of the output, so strip that.
            if pdb_cache_path.endswith("."):
              pdb_cache_path = pdb_cache_path[:-1]
        if strip_and_translate and not pdb_cache_path:
          # Look for locally built symbols
          if os.path.exists(path):
            pdb_cache_path = path
            local_symbol_files.append(path)
        if pdb_cache_path:
          if strip_and_translate:
            tempdir = tempfile.mkdtemp()
            tempdirs.append(tempdir)
            dest_path = os.path.join(tempdir, os.path.basename(pdb_cache_path))
            print("  Copying PDB to %s" % dest_path)
            # For some reason putting quotes around the command to be run causes
            # it to fail. So don't do that.
            copy_command = '%s "%s" "%s" -p' % (pdbcopy_path, pdb_cache_path, dest_path)
            print("  > %s" % copy_command)
            if un_fastlink_tool:
              # If the un_fastlink_tool is available then run the pdbcopy command in a
              # try block. If pdbcopy fails then run the un_fastlink_tool and try again.
              try:
                output = str(subprocess.check_output(copy_command, stderr=subprocess.STDOUT))
                if output:
                  print("  %s" % output, end="")
              except:
                convert_command = '%s "%s"' % (un_fastlink_tool, pdb_cache_path)
                print("Attempting to un-fastlink PDB so that pdbcopy can strip it. This may be slow.")
                print("  > %s" % convert_command)
                subprocess.check_output(convert_command)
                output = str(subprocess.check_output(copy_command, stderr=subprocess.STDOUT))
                if output:
                  print("  %s" % output, end="")
            else:
              output = str(subprocess.check_output(copy_command, stderr=subprocess.STDOUT))
              if output:
                print("  %s" % output, end="")
            if not os.path.exists(dest_path):
              print("Aborting symbol generation because stripped PDB '%s' does not exist. WPA symbol loading may be slow." % dest_path)
              sys.exit(0)
          else:
            print("   Symbols retrieved.")
        else:
          print("  Failed to retrieve symbols.")

  if tempdirs:
    symbol_path = ";".join(tempdirs)
    print("Stripped PDBs are in %s. Converting to symcache files now." % symbol_path)
    os.environ["_NT_SYMBOL_PATH"] = symbol_path
    # Create a list of to/from renamed tuples
    renames = []
    error = False
    try:
      rename_errors = False
      for local_pdb in local_symbol_files:
        temp_name = local_pdb + "x"
        print("Renaming %s to %s to stop unstripped PDBs from being used." % (local_pdb, temp_name))
        try:
          # If the destination file exists we have to rename it or else the
          # rename will fail.
          if os.path.exists(temp_name):
            os.remove(temp_name)
          os.rename(local_pdb, temp_name)
        except:
          # Rename can and does throw exceptions. We must catch and continue.
          e = sys.exc_info()[0]
          print("Hit exception while renaming %s to %s. Continuing.\n%s" % (local_pdb, temp_name, e))
          rename_errors = True
        else:
          renames.append((local_pdb, temp_name))

      #-build = build the symcache store for this trace (see xperf -help symcache)
      if rename_errors:
        print("Skipping symbol generation due to PDB rename errors. WPA symbol loading may be slow.")
      else:
        gen_command = 'xperf -i "%s" -symbols -tle -tti -a symcache -build' % tracename
        print("> %s" % gen_command)
        for line in os.popen(gen_command).readlines():
          pass # Don't print line
    except KeyboardInterrupt:
      # Catch Ctrl+C exception so that PDBs will get renamed back.
      if renames:
        print("Ctrl+C detected. Renaming PDBs back.")
      error = True
    for rename_names in renames:
      try:
        os.rename(rename_names[1], rename_names[0])
      except:
        # Rename can and does throw exceptions. We must catch and continue.
        e = sys.exc_info()[0]
        print("Hit exception while renaming %s back. Continuing.\n%s" % (rename_names[1], e))
    for symcache_file in symcache_files:
      if os.path.exists(symcache_file):
        print("%s generated." % symcache_file)
      else:
        print("Error: %s not generated." % symcache_file)
        error = True
    # Delete the stripped PDB files
    if error:
      print("Retaining PDBs to allow rerunning xperf command-line.")
      print("If re-running the command be sure to go:")
      print("set _NT_SYMBOL_PATH=%s" % symbol_path)
    else:
      for directory in tempdirs:
        shutil.rmtree(directory, ignore_errors=True)
  elif strip_and_translate:
    if found_uncached:
      print("No PDBs copied, nothing to do.")
    else:
      print("No uncached PDBS found, nothing to do.")

if __name__ == "__main__":
  main()
