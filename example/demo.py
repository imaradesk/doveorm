"""
example/demo.py
A tour of DoveORM on a small blog: models, migrations, queries and relationships.

The example is a self-contained project:

    example/
    ├── doveorm.toml          project settings for the `doveorm` command
    ├── blog/models.py        User, Profile, Post, Tag, PostTag
    ├── blog/settings.py      database settings (blog.db next to doveorm.toml)
    ├── migrations/           created by `doveorm generate`
    └── demo.py               this file

Try the workflow from inside example/:

    doveorm apply             # create the tables
    python demo.py            # run the tour (safe to run again)
    doveorm status            # what the database looks like now
    doveorm doctor            # health checks

Change a model in blog/models.py, then `doveorm generate` writes the next
migration and `doveorm apply` runs it.
"""
import datetime
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
# Lets the demo run from a clone of the repository without installing DoveORM.
sys.path.insert(0, os.path.join(HERE, ".."))

import doveorm
from doveorm import migrations

from blog.settings import DOVEORM

# ─────────────────────────────────────────────────────────
# 1.  Configure the database and bring the schema up to date
# ─────────────────────────────────────────────────────────
db = doveorm.configure(**DOVEORM)

from blog.models import Post, Profile, Tag, User   # noqa: E402  (after configure)

# Same as running `doveorm apply` in this folder; does nothing when up to date.
applied = migrations.migrate(os.path.join(HERE, "migrations"))
print(f"Migrations applied this run: {[name for name, _ in applied] or 'none'}\n")


# ─────────────────────────────────────────────────────────
# 2.  Create records
# ─────────────────────────────────────────────────────────
# Start from a clean slate so every run prints the same thing.  all() is the
# explicit opt-in for touching every row; ON DELETE CASCADE removes the
# users' posts, profiles and post_tags rows too.
User.all().delete()
Tag.all().delete()

NOW = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)   # naive UTC

titus = User.create(name="Titus", email="titus@example.com", age=28, created_at=NOW)
alice = User.create(name="Alice", email="alice@example.com", age=22)
bob   = User.create(name="Bob",   email="bob@example.com",   age=35, active=False)

Profile.create(user_id=titus.id, bio="Builder of things.")
Profile.create(user_id=alice.id, bio="Designer and dreamer.")

python_tag = Tag.create(name="python")
orm_tag    = Tag.create(name="orm")
db_tag     = Tag.create(name="databases")

post1 = Post.create(user_id=titus.id, title="Building a Python ORM",
                    body="Here's how I built DoveORM...", published=True, created_at=NOW)
post2 = Post.create(user_id=titus.id, title="SQLite Tips", body="WAL mode is great...")
post3 = Post.create(user_id=alice.id, title="Design Systems", published=True)

# Rows in the post_tags join table (already-linked pairs are ignored)
post1.tags.add(python_tag, orm_tag)
post2.tags.add(db_tag)
post3.tags.add(python_tag)

print("=== Records created ===\n")


def names(users):
    return [u.name for u in users]


def titles(posts):
    return [p.title for p in posts]


# ─────────────────────────────────────────────────────────
# 3.  Querying
# ─────────────────────────────────────────────────────────
print("── User.where(name='Titus', age=28).get()")
print(names(User.where(name="Titus", age=28).get()), "\n")

print("── User.where(age__gte=25).order_by('-age').get()")
print(names(User.where(age__gte=25).order_by("-age").get()), "\n")

print("── User.where(active=True).count()")
print(User.where(active=True).count(), "\n")

print("── User.all().order_by('name').limit(2).get()")
print(names(User.all().order_by("name").limit(2).get()), "\n")

print("── User.find(titus.id)")
print(User.find(titus.id).name, "\n")

print("── User.where(age__in=[22, 35]).get()")
print(names(User.where(age__in=[22, 35]).get()), "\n")

print("── Post.where(published=True).get()")
print(titles(Post.where(published=True).get()), "\n")


# ─────────────────────────────────────────────────────────
# 4.  Relationships
# ─────────────────────────────────────────────────────────
print("── titus.posts  (HasMany)")
print(titles(titus.posts), "\n")

print("── titus.profile  (HasOne)")
print(titus.profile.bio, "\n")

print("── post1.author  (BelongsTo)")
print(post1.author.name, "\n")

print("── post1.tags  (ManyToMany)")
print([t.name for t in post1.tags.all()], "\n")

print("── python_tag.posts  (ManyToMany, other side)")
print(titles(python_tag.posts.all()), "\n")


# ─────────────────────────────────────────────────────────
# 5.  Update / delete
# ─────────────────────────────────────────────────────────
print("── Bulk update: deactivate users younger than 25")
print(f"Updated {User.where(age__lt=25).update(active=False)} row(s)\n")

print("── Instance update: Bob's birthday")
bob.age = 36
bob.save()
print({k: v for k, v in User.find(bob.id).to_dict().items() if k != "id"}, "\n")

print("── get_or_create: the first call creates, the second finds")
for _ in range(2):
    zara, created = User.get_or_create(email="zara@example.com",
                                       defaults={"name": "Zara", "age": 19})
    print(f"created={created}  name={zara.name}")
print()

print("── Delete 'SQLite Tips' (its post_tags rows go with it: ON DELETE CASCADE)")
post2.delete()
print("Posts remaining:", titles(Post.all().order_by("id").get()), "\n")

db.close_all()
print("All done! ✓")
