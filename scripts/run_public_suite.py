import subprocess, sys
raise SystemExit(subprocess.call([sys.executable, "-m", "pytest", "-q", "tests/public"]))
