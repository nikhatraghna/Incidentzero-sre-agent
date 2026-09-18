from pathlib import Path
count = 0
for p in Path("incidentzero").rglob("*.py"):
    for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if "TODO(A1)" in line:
            print(f"{p}:{i}: {line.strip()}")
            count += 1
print(f"\nAssignment TODO markers remaining: {count}")
