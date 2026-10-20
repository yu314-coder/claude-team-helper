#!/bin/zsh
# Build the Windows version on this Mac with py2bin, as one self-extracting exe.
#   ./build_windows.sh            → dist/ClaudeTeamHelper.exe (one file)
# Needs: uv, python3 with pip, and py2bin (pip install python-to-binary, or set PY2BIN_SRC
# to a python_to_binary checkout's src/ folder).
set -e
cd "${0:A:h}"

PYVER=3.12.10                 # python.org embeddable CPython carried inside the app
PYTAG=${${PYVER%.*}//./}      # 312
PY2BIN_SRC=${PY2BIN_SRC:-/Volumes/D/github/python_to_binary/src}
W=build/windows
rm -rf $W && mkdir -p $W/src $W/wheels dist

# 1. The app and the MCP server it carries.
cp windows/app.py windows/sync_engine.py mcp/computer_control.py $W/src/

# 2. Windows wheels. uv resolves with Windows markers (pywebview needs pythonnet there);
#    pip fetches each one for win_amd64, or builds the pure-Python ones that only ship sdists.
uv pip compile --quiet --no-header --no-annotate --python-platform x86_64-pc-windows-msvc \
  --python-version ${PYVER%.*} - <<< $'pywebview\npyautogui\npillow' | grep -v '^\s*#' > $W/requirements.txt
echo "Windows packages:"; sed 's/^/  /' $W/requirements.txt
while read -r req; do
  [[ -z $req ]] && continue
  python3 -m pip download --quiet --disable-pip-version-check --no-deps --only-binary=:all: --platform win_amd64 \
    --python-version $PYTAG --implementation cp -d $W/wheels "$req" 2>/dev/null ||
  python3 -m pip wheel --quiet --disable-pip-version-check --no-deps -w $W/wheels "$req"
done < $W/requirements.txt

# 3. Freeze: a windowed exe beside its own CPython, one folder.
[[ -f build/icon1024.png ]] || swift icon.swift build/icon1024.png
for n in 16 32 48 256; do sips -z $n $n build/icon1024.png --out $W/icon_$n.png >/dev/null; done
python3 - $W <<'EOF'   # pack the PNGs into an .ico (Vista+ icons may hold PNG data directly)
import struct, sys, pathlib
w = pathlib.Path(sys.argv[1]); sizes = [16, 32, 48, 256]
blobs = [(w / f"icon_{n}.png").read_bytes() for n in sizes]
out, off = struct.pack("<HHH", 0, 1, len(sizes)), 6 + 16 * len(sizes)
for n, b in zip(sizes, blobs):
    out += struct.pack("<BBBBHHII", n % 256, n % 256, 0, 0, 1, 32, len(b), off); off += len(b)
(w / "icon.ico").write_bytes(out + b"".join(blobs))
EOF
PY2BIN=(python3 -m py2bin)
python3 -c "import py2bin" 2>/dev/null || export PYTHONPATH=$PY2BIN_SRC
"${PY2BIN[@]}" freeze $W/src/app.py --source-root $W/src \
  --target windows-x86_64 --auto-fetch --fetch-python $PYVER --wheel-dir $W/wheels \
  --exclude AppKit --exclude Quartz --exclude Foundation --exclude objc \
  --app --name "ClaudeTeamHelper" --icon $W/icon.ico --compact --onedir \
  --output $W/ClaudeTeamHelper --clean

# 4. Drop what the app never loads: Pillow's optional codecs (it imports them only if present)
#    and the database / installer modules. About 13 MB.
B=$W/ClaudeTeamHelper
rm -f $B/site-packages/PIL/_{avif,imagingft,webp,imagingcms,imagingtk,imagingmath,imagingmorph}.*.pyd
rm -f $B/runtime/{sqlite3.dll,_sqlite3.pyd,_msi.pyd}
find $B -name __pycache__ -type d -prune -exec rm -rf {} +

# 5. Pack the folder into one self-extracting exe (py2bin's one-file stub). On first launch it
#    unpacks to %LOCALAPPDATA%\py2bin\<hash> and runs from there afterwards.
rm -f dist/ClaudeTeamHelper-windows-x64.zip dist/ClaudeTeamHelper.exe
python3 - $B $W/icon.ico dist/ClaudeTeamHelper.exe <<'EOF'
import sys
from pathlib import Path
from py2bin.onefile import create_onefile
root, icon, out = map(Path, sys.argv[1:4])
r = create_onefile(root, out, target="windows-x86_64", launcher=root / "runtime" / "ClaudeTeamHelper.exe",
                   icon=icon, windows_windowed=True)
print(f"one-file exe: {r.artifact} (payload sha256 {r.payload_sha256[:12]})")
EOF
du -sh $B dist/ClaudeTeamHelper.exe

# Intermediate wheels are only needed during the build.
rm -rf $W/wheels $W/src $W/icon_*.png
