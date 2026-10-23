"""Blog models: users, profiles, posts and tags, with every relationship type."""
from doveorm import (
    Model, AutoField, CharField, IntegerField, TextField, BooleanField,
    DateTimeField, ForeignKey, HasMany, HasOne, BelongsTo, ManyToMany,
)


class User(Model):
    id         = AutoField()
    name       = CharField(max_length=100, nullable=False)
    email      = CharField(max_length=200, nullable=False, unique=True)
    age        = IntegerField(default=0)
    active     = BooleanField(default=True)
    created_at = DateTimeField(nullable=True)

    # Relationships
    posts   = HasMany("Post",   foreign_key="user_id")
    profile = HasOne("Profile", foreign_key="user_id")


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


class PostTag(Model):
    """The join table behind Post.tags / Tag.posts, so migrations create it too."""
    id      = AutoField()
    post_id = ForeignKey("Post", on_delete="CASCADE", nullable=False)
    tag_id  = ForeignKey("Tag",  on_delete="CASCADE", nullable=False)

    class Meta:
        table_name = "post_tags"
        unique_together = [("post_id", "tag_id")]   # a post is tagged once per tag
