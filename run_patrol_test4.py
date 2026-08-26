import sys
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(".env"), override=True)
import runpy
sys.argv = ["run_real_patrol_batch", "--units-file", "/tmp/unit_test4.json", "--count", "4", "--concurrency", "4", "--skip-live-precheck"]
runpy.run_module("scripts.run_real_patrol_batch", run_name="__main__")
