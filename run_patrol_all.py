import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(".env"), override=True)
print("PATROL_DATABASE_URL set:", bool(os.environ.get("PATROL_DATABASE_URL")))
import runpy
import sys
sys.argv = ["run_real_patrol_batch", "--all-active", "--concurrency", "32", "--skip-live-precheck"]
runpy.run_module("scripts.run_real_patrol_batch", run_name="__main__")
