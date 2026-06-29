from django.db import models


class UserDB(models.Model):
    id = models.AutoField(primary_key=True)
    email = models.CharField(max_length=255, unique=True)
    username = models.CharField(max_length=255)
    hashed_password = models.CharField(max_length=255)
    avatar = models.CharField(max_length=10, default="🧑")
    created_at = models.DateTimeField()
    banned = models.BooleanField(default=False)
    admin_notes = models.TextField(blank=True, default="")

    class Meta:
        managed = False
        db_table = 'users'

    def __str__(self):
        return f"{self.username} ({self.email})"


class ChatHistoryDB(models.Model):
    id = models.AutoField(primary_key=True)
    user_email = models.CharField(max_length=255)
    session_title = models.CharField(max_length=255, default="New Chat")
    messages = models.TextField(default="[]")
    created_at = models.DateTimeField()
    updated_at = models.DateTimeField()
    tags = models.CharField(max_length=255, blank=True, default="")
    bookmarked = models.BooleanField(default=False)

    class Meta:
        managed = False
        db_table = 'chat_history'

    def __str__(self):
        return self.session_title


class AdminLog(models.Model):
    ACTION_CHOICES = [
        ('delete_user', 'Deleted User'),
        ('delete_session', 'Deleted Session'),
        ('ban_user', 'Banned User'),
        ('unban_user', 'Unbanned User'),
        ('bookmark', 'Bookmarked Session'),
        ('export', 'Exported Data'),
        ('other', 'Other'),
    ]
    admin_user = models.CharField(max_length=150)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES)
    target = models.CharField(max_length=255)
    detail = models.TextField(blank=True)
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True

    def __str__(self):
        return f"{self.timestamp} - {self.admin_user} - {self.action}"


class FailedLoginDB(models.Model):
    email = models.CharField(max_length=255)
    ip = models.CharField(max_length=64, blank=True, default="")
    timestamp = models.DateTimeField(auto_now_add=True)

    class Meta:
        managed = True
        db_table = 'failed_logins'

    def __str__(self):
        return f"{self.email} @ {self.timestamp}"