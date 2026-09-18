from pathlib import Path
import hashlib

root = Path(__file__).resolve().parents[1]
manifest = root / "PROTECTED_FILES.sha256"
failed = []
for line in manifest.read_text(encoding="utf-8").splitlines():
    digest, rel = line.split("  ", 1)
    path = root / rel
    actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"
    if actual != digest:
        failed.append((rel, digest, actual))
if failed:
    print("Protected starter files differ from the distributed version:")
    for rel, expected, actual in failed:
        print(f"  {rel}\n    expected {expected}\n    actual   {actual}")
    raise SystemExit(2)
print("Protected starter files match the distributed version.")
