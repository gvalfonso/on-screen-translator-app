"""Build Snip Translator into a standalone Windows app + installer.

    python build.py                 -> dist/SnipTranslator/SnipTranslator.exe  +  dist/SnipTranslator-Setup-<ver>.exe
    python build.py --no-installer  -> just the exe folder

Version comes from __version__ in translator.py: bump it there, then build.
Needs: pip install pyinstaller; Inno Setup 6 (winget install JRSoftware.InnoSetup) for the installer.
"""
import os, re, shutil, subprocess, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
os.chdir(ROOT)
VERSION = re.search(r'^__version__ = "([\d.]+)"', open("translator.py", encoding="utf-8").read(), re.M).group(1)
NUMS = (VERSION.split(".") + ["0"] * 4)[:4]

# Windows version resource: shows in the exe's Properties > Details
VERSION_INFO = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({", ".join(NUMS)}), prodvers=({", ".join(NUMS)}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
          StringStruct('CompanyName', 'George'),
          StringStruct('FileDescription', 'Snip Translator'),
          StringStruct('FileVersion', '{VERSION}'),
          StringStruct('InternalName', 'SnipTranslator'),
          StringStruct('OriginalFilename', 'SnipTranslator.exe'),
          StringStruct('ProductName', 'Snip Translator'),
          StringStruct('ProductVersion', '{VERSION}')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def main():
    print(f"== building Snip Translator v{VERSION}")
    shutil.rmtree("build", ignore_errors=True)
    shutil.rmtree(os.path.join("dist", "SnipTranslator"), ignore_errors=True)
    os.makedirs("build", exist_ok=True)
    with open(os.path.join("build", "version_info.txt"), "w", encoding="utf-8") as f:
        f.write(VERSION_INFO)

    subprocess.run([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
                    "--windowed",  # no console window
                    "--onedir",    # folder build: starts fast (matters for a start-with-Windows tray app)
                    "--name", "SnipTranslator", "--icon", os.path.join(ROOT, "icon.ico"),  # absolute: specpath is build/
                    "--version-file", os.path.join(ROOT, "build", "version_info.txt"),
                    "--hidden-import", "pystray._win32",  # pystray picks its backend at runtime
                    # optional deps Pillow/requests would drag in but we never use (~46 MB)
                    "--exclude-module", "numpy", "--exclude-module", "cryptography",
                    "--distpath", "dist", "--workpath", "build", "--specpath", "build",
                    os.path.join(ROOT, "translator.py")], check=True)
    print(f"== exe: dist/SnipTranslator/SnipTranslator.exe")

    if "--no-installer" in sys.argv:
        return
    iscc = next((p for p in [shutil.which("ISCC"),
                             r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                             os.path.expandvars(r"%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe")]
                 if p and os.path.exists(p)), None)
    if not iscc:
        sys.exit("Inno Setup not found (winget install JRSoftware.InnoSetup) - exe folder is built, installer skipped")
    subprocess.run([iscc, f"/DAppVersion={VERSION}", "installer.iss"], check=True)
    print(f"== installer: dist/SnipTranslator-Setup-{VERSION}.exe")


if __name__ == "__main__":
    main()
