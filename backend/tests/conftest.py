import os

from app.core.config import Settings

Settings.model_config["env_file"] = None

# Offline tests use fake provider values and never enter the application lifespan.
os.environ["APP_ENV"] = "development"
os.environ["MONGO_URI"] = "mongodb://localhost:27017"
os.environ["GEMINI_API_KEY"] = "offline-test-key"
os.environ["LLM_PROVIDER"] = "gemini"
os.environ["JWT_SECRET_KEY"] = "offline-test-secret-for-unit-testing-only"
