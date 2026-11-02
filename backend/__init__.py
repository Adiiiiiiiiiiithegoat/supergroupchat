import os

from dotenv import load_dotenv

# before any submodule reads os.getenv at import time (db, auth, main)
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))