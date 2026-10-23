"""Database settings for the example (doveorm.toml points here)."""
import os

_HERE = os.path.dirname(os.path.abspath(__file__))

DOVEORM = {
    # BLOG_DB overrides the location; by default blog.db sits next to doveorm.toml.
    "DATABASE_PATH": os.environ.get("BLOG_DB", os.path.join(_HERE, "..", "blog.db")),
}
