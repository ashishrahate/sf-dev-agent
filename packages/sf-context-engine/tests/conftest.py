"""Keep the suite hermetic: never download a model or call a hosted embedding API."""

import os

os.environ["SF_CONTEXT_EMBEDDER"] = "mock"
