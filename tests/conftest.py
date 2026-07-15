"""Keep local Docker `.env` values from redirecting tests to container paths."""

import os


os.environ["GERRY_DATA_DIR"] = "/tmp/gerrymandering-tests"
os.environ["GERRY_DATABASE_URL"] = "sqlite:////tmp/gerrymandering-tests/gerry.db"
os.environ["GERRY_INLINE_WORKER"] = "true"
