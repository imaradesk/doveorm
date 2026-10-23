"""
examples/blog.py
A complete example: Users, Posts, Tags with all relationship types.
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from doveorm import (
    Database, Model, Migrator, AutoField,
    CharField, IntegerField, TextField, BooleanField,
    DateTimeField, ForeignKey,
    HasMany, HasOne, BelongsTo, ManyToMany,
)
import datetime

# ─────────────────────────────────────────────────────────
# 1.  Database setup
# ─────────────────────────────────────────────────────────
db = Database("app.db")   # swap to "blog.db" for a real file


# ─────────────────────────────────────────────────────────
# 2.  Define models
# ─────────────────────────────────────────────────────────

class User(Model):
    id         = AutoField()
    name       = CharField(max_length=100, nullable=False)
    email      = CharField(max_length=200, nullable=False, unique=True)
    age        = IntegerField(default=0)
    active     = BooleanField(default=True)
    created_at = DateTimeField(nullable=True)

    # Relationships
    posts   = HasMany("Post",    foreign_key="user_id")
    profile = HasOne("Profile",  foreign_key="user_id")


class Profile(Model):
    id      = AutoField()
    user_id = ForeignKey("User", on_delete="CASCADE", nullable=False)
    bio     = TextField(nullable=True)

    # Relationships
    user = BelongsTo("User", foreign_key="user_id")


class Tag(Model):
    id   = AutoField()
    name = CharField(max_length=50, nullable=False, unique=True)

    posts = ManyToMany("Post", through="post_tags",
                       source_fk="tag_id", target_fk="post_id")


class Post(Model):
    id         = AutoField()
    user_id    = ForeignKey("User", on_delete="CASCADE", nullable=False)
    title      = CharField(max_length=200, nullable=False)
    body       = TextField(nullable=True)
    published  = BooleanField(default=False)
    created_at = DateTimeField(nullable=True)

    # Relationships
    author = BelongsTo("User", foreign_key="user_id")
    tags   = ManyToMany("Tag", through="post_tags",
                        source_fk="post_id", target_fk="tag_id")


# ─────────────────────────────────────────────────────────
# 3.  Wire DB + run migrations
# ─────────────────────────────────────────────────────────
for cls in [User, Profile, Tag, Post]:
    cls.use(db)

migrator = Migrator(db)
migrator.register("0001_create_users",   lambda: User.create_table())
migrator.register("0002_create_profiles",lambda: Profile.create_table())
migrator.register("0003_create_tags",    lambda: Tag.create_table())
migrator.register("0004_create_posts",   lambda: Post.create_table())
migrator.register("0005_post_tags_join", lambda: db.execute_script("""
    CREATE TABLE IF NOT EXISTS post_tags (
        post_id INTEGER NOT NULL REFERENCES posts(id) ON DELETE CASCADE,
        tag_id  INTEGER NOT NULL REFERENCES tags(id)  ON DELETE CASCADE,
        PRIMARY KEY (post_id, tag_id)
    );
"""))
migrator.migrate()

print()

# ─────────────────────────────────────────────────────────
# 4.  Create records
# ─────────────────────────────────────────────────────────
titus = User.create(
    name="Titus",
    email="titus@example.com",
    age=28,
    active=True,
    created_at=datetime.datetime.utcnow(),
)
alice = User.create(name="Alice", email="alice@example.com", age=22, active=True)
bob   = User.create(name="Bob",   email="bob@example.com",   age=35, active=False)

Profile.create(user_id=titus.id, bio="Builder of things.")
Profile.create(user_id=alice.id, bio="Designer and dreamer.")

python_tag = Tag.create(name="python")
orm_tag    = Tag.create(name="orm")
db_tag     = Tag.create(name="databases")

post1 = Post.create(
    user_id=titus.id,
    title="Building a Python ORM",
    body="Here's how I built DoveORM...",
    published=True,
    created_at=datetime.datetime.utcnow(),
)
post2 = Post.create(
    user_id=titus.id,
    title="SQLite Tips",
    body="WAL mode is great...",
    published=False,
)
post3 = Post.create(user_id=alice.id, title="Design Systems", published=True)

# Link tags via join table
for (pid, tid) in [(post1.id, python_tag.id), (post1.id, orm_tag.id),
                   (post2.id, db_tag.id),     (post3.id, python_tag.id)]:
    db.execute_write("INSERT INTO post_tags (post_id, tag_id) VALUES (?, ?)", [pid, tid])

print("=== Records created ===\n")


# ─────────────────────────────────────────────────────────
# 5.  Querying
# ─────────────────────────────────────────────────────────

print("── .where(name='Titus', age=28).get()")
result = User.where(name="Titus", age=28).get()
print(result, "\n")

print("── .where(age__gte=25).order_by('-age').get()")
result = User.where(age__gte=25).order_by("-age").get()
print(result, "\n")

print("── .where(active=True).count()")
print(User.where(active=True).count(), "\n")

print("── .all().limit(2).get()")
print(User.all().limit(2).get(), "\n")

print("── User.find(1)")
print(User.find(1), "\n")

print("── User.where(age__in=[22, 35]).get()")
print(User.where(age__in=[22, 35]).get(), "\n")

print("── Post.where(published=True).get()")
print(Post.where(published=True).get(), "\n")


# ─────────────────────────────────────────────────────────
# 6.  Relationships
# ─────────────────────────────────────────────────────────

print("── titus.posts  (HasMany)")
print(titus.posts, "\n")

print("── titus.profile  (HasOne)")
print(titus.profile, "\n")

print("── post1.author  (BelongsTo)")
print(post1.author, "\n")

print("── post1.tags  (ManyToMany)")
print(post1.tags, "\n")

print("── python_tag.posts  (ManyToMany reverse)")
print(python_tag.posts, "\n")


# ─────────────────────────────────────────────────────────
# 7.  Update / Delete
# ─────────────────────────────────────────────────────────

print("── Bulk update: deactivate users with age < 25")
n = User.where(age__lt=25).update(active=False)
print(f"Updated {n} row(s)\n")

print("── Instance update: change Bob's age")
bob.age = 36
bob.save()
print(User.find(bob.id).to_dict(), "\n")

print("── get_or_create")
user, created = User.get_or_create(
    defaults={"age": 19, "active": True},
    name="Zara",
    email="zara@example.com",
)
print(f"created={created}  user={user}\n")

print("── Delete post2")
post2.delete()
print("Posts remaining:", Post.all().count(), "\n")

print("All done! ✓")