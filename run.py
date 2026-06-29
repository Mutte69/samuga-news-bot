"""
Samuga Travels — Crash-resistant launcher.
If bot.py crashes, waits 5 seconds and restarts automatically.
Railway will keep this process alive forever.
"""
import subprocess
import sys
import time
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger("launcher")

MAX_RESTARTS = 10
RESTART_DELAY = 5  # seconds between restarts

restarts = 0

while True:
    logger.info(f"🚀 Starting Samuga Travels Bot (attempt {restarts + 1})...")
    
    process = subprocess.run(
        [sys.executable, "bot.py"],
        # Don't capture output — let it stream to Railway logs
    )
    
    exit_code = process.returncode
    
    if exit_code == 0:
        logger.info("✅ Bot exited cleanly.")
        break
    
    restarts += 1
    logger.error(f"❌ Bot crashed with exit code {exit_code}. Restart {restarts}/{MAX_RESTARTS} in {RESTART_DELAY}s...")
    
    if restarts >= MAX_RESTARTS:
        logger.critical("🛑 Too many crashes. Stopping launcher to prevent loop.")
        sys.exit(1)
    
    time.sleep(RESTART_DELAY)
