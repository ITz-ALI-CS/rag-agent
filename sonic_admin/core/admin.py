import os
import json
import csv
import secrets
import urllib.request
from datetime import timedelta
from django.contrib import admin
from django.http import HttpResponse, FileResponse, HttpResponseRedirect
from django.shortcuts import render
from django.urls import path
from django.conf import settings
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe
from django.db.models import Q
from passlib.context import CryptContext
from .models import UserDB, ChatHistoryDB, AdminLog, FailedLoginDB

admin.site.site_header = "⚡ Sonic AI Admin"
admin.site.site_title = "Sonic AI Admin"
admin.site.index_title = "Unstoppable 1.0 — Control Panel"

pwd_context = CryptContext(schemes=["sha256_crypt"], deprecated="auto")


def nav_context(active):
    base = "padding:8px 18px;border-radius:8px;font-size:13px;text-decoration:none;font-weight:700;display:inline-flex;align-items:center;gap:6px;transition:all .2s;"
    on = base + "background:linear-gradient(135deg,rgba(0,212,255,.3),rgba(245,166,35,.2));border:2px solid #00d4ff;color:#00d4ff;animation:navPulse 2s ease-in-out infinite;"
    off = base + "background:#0c1a2c;border:2px solid #1a3050;color:#6a9ec0;"
    return {
        'nav_dash': on if active == 'dashboard' else off,
        'nav_analytics': on if active == 'analytics' else off,
        'nav_system': on if active == 'system' else off,
        'nav_security': on if active == 'security' else off,
    }


PROJECT_DIR = r'C:\Users\HP\sonic-ai'
DATA_DIR = os.path.join(PROJECT_DIR, 'data')
VECTORSTORE_DIR = os.path.join(PROJECT_DIR, 'vectorstore')
ENV_PATH = os.path.join(PROJECT_DIR, '.env')
UNSAFE_KEYWORDS = ["porn", "sex", "nude", "naked", "18+", "xxx", "erotic", "explicit", "nsfw"]


def log_action(request, action, target, detail=""):
    AdminLog.objects.create(admin_user=request.user.username, action=action, target=target, detail=detail)


def export_csv(self, request, queryset):
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = f'attachment; filename="{self.model.__name__}_export.csv"'
    writer = csv.writer(response)
    fields = [f.name for f in self.model._meta.fields]
    writer.writerow(fields)
    for obj in queryset:
        writer.writerow([getattr(obj, f) for f in fields])
    return response
export_csv.short_description = "⬇️ Export selected to CSV"


# ───────────────────────── Model admins ─────────────────────────

@admin.register(UserDB)
class UserDBAdmin(admin.ModelAdmin):
    list_display = ('username', 'email', 'avatar', 'banned', 'created_at')
    search_fields = ('username', 'email')
    list_filter = ('created_at', 'banned')
    ordering = ('-created_at',)
    fields = ('username', 'email', 'avatar', 'banned', 'admin_notes', 'created_at')
    readonly_fields = ('created_at',)
    actions = ['ban_users', 'unban_users', 'reset_passwords', 'delete_inactive_users', export_csv]

    def ban_users(self, request, queryset):
        for u in queryset:
            u.banned = True; u.save(); log_action(request, 'ban_user', u.email)
        self.message_user(request, f"Banned {queryset.count()} user(s).")
    ban_users.short_description = "🚫 Ban selected users"

    def unban_users(self, request, queryset):
        for u in queryset:
            u.banned = False; u.save(); log_action(request, 'unban_user', u.email)
        self.message_user(request, f"Unbanned {queryset.count()} user(s).")
    unban_users.short_description = "✅ Unban selected users"

    def reset_passwords(self, request, queryset):
        results = []
        for u in queryset:
            new_pw = secrets.token_urlsafe(8)
            u.hashed_password = pwd_context.hash(new_pw); u.save()
            log_action(request, 'other', u.email, detail="Password reset by admin")
            results.append(f"{u.email}: {new_pw}")
        self.message_user(request, "New passwords (copy now, shown once): " + " | ".join(results))
    reset_passwords.short_description = "🔑 Reset password (generates new one)"

    def delete_inactive_users(self, request, queryset):
        cutoff = timezone.now() - timedelta(days=30)
        active_emails = set(ChatHistoryDB.objects.values_list('user_email', flat=True))
        deleted = 0
        for u in queryset:
            if u.email not in active_emails and u.created_at < cutoff:
                log_action(request, 'delete_user', u.email, detail="Bulk inactive cleanup")
                u.delete(); deleted += 1
        self.message_user(request, f"Deleted {deleted} inactive user(s) (no sessions, 30+ days old).")
    delete_inactive_users.short_description = "🗑️ Delete inactive users (no sessions, 30+ days)"

    def delete_model(self, request, obj):
        log_action(request, 'delete_user', obj.email); super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            log_action(request, 'delete_user', obj.email)
        super().delete_queryset(request, queryset)


@admin.register(ChatHistoryDB)
class ChatHistoryDBAdmin(admin.ModelAdmin):
    list_display = ('session_title', 'user_email', 'message_count', 'bookmarked', 'tags', 'updated_at')
    search_fields = ('session_title', 'user_email', 'messages', 'tags')
    list_filter = ('created_at', 'bookmarked')
    ordering = ('-updated_at',)
    readonly_fields = ('formatted_messages',)
    fields = ('session_title', 'user_email', 'tags', 'bookmarked', 'formatted_messages', 'created_at', 'updated_at')
    actions = ['bookmark_sessions', 'unbookmark_sessions', export_csv]

    def message_count(self, obj):
        try: return len(json.loads(obj.messages))
        except Exception: return 0
    message_count.short_description = "Messages"

    def bookmark_sessions(self, request, queryset):
        for s in queryset:
            s.bookmarked = True; s.save(); log_action(request, 'bookmark', s.session_title)
        self.message_user(request, f"Bookmarked {queryset.count()} session(s).")
    bookmark_sessions.short_description = "⭐ Bookmark selected"

    def unbookmark_sessions(self, request, queryset):
        for s in queryset:
            s.bookmarked = False; s.save()
        self.message_user(request, f"Unbookmarked {queryset.count()} session(s).")
    unbookmark_sessions.short_description = "☆ Unbookmark selected"

    def formatted_messages(self, obj):
        try: msgs = json.loads(obj.messages)
        except Exception: return "Could not parse messages."
        html = '<div style="max-width:700px;">'
        for m in msgs:
            role = m.get("role", ""); content = escape(m.get("content", ""))
            if role == "user":
                html += f'<div style="text-align:right;margin:8px 0;"><span style="background:#0099cc;color:#fff;padding:8px 14px;border-radius:14px;display:inline-block;max-width:80%;">{content}</span></div>'
            else:
                html += f'<div style="text-align:left;margin:8px 0;"><span style="background:#0c1a2c;color:#dff0ff;border:1px solid #1a3050;padding:8px 14px;border-radius:14px;display:inline-block;max-width:80%;">{content}</span></div>'
        html += '</div>'
        return mark_safe(html)
    formatted_messages.short_description = "Conversation"

    def delete_model(self, request, obj):
        log_action(request, 'delete_session', obj.session_title); super().delete_model(request, obj)

    def delete_queryset(self, request, queryset):
        for obj in queryset:
            log_action(request, 'delete_session', obj.session_title)
        super().delete_queryset(request, queryset)


@admin.register(AdminLog)
class AdminLogAdmin(admin.ModelAdmin):
    list_display = ('admin_user', 'action', 'target', 'timestamp')
    list_filter = ('action',)
    ordering = ('-timestamp',)


@admin.register(FailedLoginDB)
class FailedLoginDBAdmin(admin.ModelAdmin):
    list_display = ('email', 'ip', 'timestamp')
    ordering = ('-timestamp',)
    search_fields = ('email', 'ip')


# ───────────────────────── Helper functions ─────────────────────────

def check_backend():
    try:
        urllib.request.urlopen("http://127.0.0.1:8000/", timeout=2)
        return True
    except Exception:
        return False


def get_doc_files():
    files = []
    if os.path.isdir(DATA_DIR):
        for fn in os.listdir(DATA_DIR):
            fp = os.path.join(DATA_DIR, fn)
            if os.path.isfile(fp):
                files.append({'name': fn, 'size_kb': round(os.path.getsize(fp) / 1024, 1)})
    return files


def get_vectorstore_info():
    if not os.path.isdir(VECTORSTORE_DIR):
        return {'exists': False, 'size_kb': 0}
    total = 0
    for root, _, files in os.walk(VECTORSTORE_DIR):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return {'exists': True, 'size_kb': round(total / 1024, 1)}


def get_flagged_sessions():
    flagged = []
    for s in ChatHistoryDB.objects.all():
        try: msgs = json.loads(s.messages)
        except Exception: continue
        for m in msgs:
            if any(k in m.get("content", "").lower() for k in UNSAFE_KEYWORDS):
                flagged.append(s); break
    return flagged


def get_env_status():
    keys = {'GROQ_API_KEY': False, 'TAVILY_API_KEY': False, 'SECRET_KEY': False}
    if os.path.isfile(ENV_PATH):
        try:
            with open(ENV_PATH, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    for k in keys:
                        if line.startswith(k + '=') and len(line.split('=', 1)[1].strip()) > 3:
                            keys[k] = True
        except Exception:
            pass
    return keys


def get_leaderboard():
    counts = []
    for u in UserDB.objects.all():
        sessions = ChatHistoryDB.objects.filter(user_email=u.email)
        total = 0
        for s in sessions:
            try: total += len(json.loads(s.messages))
            except Exception: pass
        counts.append({'username': u.username, 'email': u.email, 'count': total})
    counts.sort(key=lambda x: -x['count'])
    return counts[:8]


def get_hourly_activity():
    hours = [0] * 24
    for s in ChatHistoryDB.objects.all():
        if s.created_at:
            hours[s.created_at.hour] += 1
    return hours


# ───────────────────────── Page views ─────────────────────────

def backup_db_view(request):
    db_path = settings.DATABASES['default']['NAME']
    return FileResponse(open(db_path, 'rb'), as_attachment=True, filename='sonic_ai_backup.db')


def delete_doc_view(request):
    fn = request.GET.get('file', '')
    safe_name = os.path.basename(fn)
    fp = os.path.join(DATA_DIR, safe_name)
    if os.path.isfile(fp):
        os.remove(fp)
        log_action(request, 'other', safe_name, detail="Document deleted via dashboard")
    return HttpResponseRedirect('/admin/system/')


def global_search_view(request):
    q = request.GET.get('q', '').strip()
    user_results, session_results = [], []
    if q:
        user_results = UserDB.objects.filter(Q(username__icontains=q) | Q(email__icontains=q))[:20]
        session_results = ChatHistoryDB.objects.filter(
            Q(session_title__icontains=q) | Q(user_email__icontains=q) | Q(messages__icontains=q)
        )[:20]
    context = admin.site.each_context(request)
    context.update({'query': q, 'user_results': user_results, 'session_results': session_results})
    return render(request, 'admin/search_results.html', context)


def dashboard_view(request):
    total_users = UserDB.objects.count()
    sessions = ChatHistoryDB.objects.all()
    total_sessions = sessions.count()
    total_messages = 0
    msg_by_day = {}
    for s in sessions:
        try: msgs = json.loads(s.messages)
        except Exception: msgs = []
        total_messages += len(msgs)
        day = s.updated_at.strftime("%Y-%m-%d") if s.updated_at else "unknown"
        msg_by_day[day] = msg_by_day.get(day, 0) + len(msgs)

    signups_by_day = {}
    for u in UserDB.objects.all():
        day = u.created_at.strftime("%Y-%m-%d") if u.created_at else "unknown"
        signups_by_day[day] = signups_by_day.get(day, 0) + 1

    today = timezone.now().date()
    days = [(today - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, -1, -1)]
    signup_series = [signups_by_day.get(d, 0) for d in days]
    message_series = [msg_by_day.get(d, 0) for d in days]

    backend_online = check_backend()
    recent_logs = AdminLog.objects.order_by('-timestamp')[:8]

    context = admin.site.each_context(request)
    context.update({
        'total_users': total_users,
        'total_sessions': total_sessions,
        'total_messages': total_messages,
        'chart_days': json.dumps(days),
        'chart_signups': json.dumps(signup_series),
        'chart_messages': json.dumps(message_series),
        'recent_logs': recent_logs,
        'backend_text': 'ONLINE' if backend_online else 'OFFLINE',
        'backend_color': '#10b981' if backend_online else '#ef4444',
        'backend_bg': 'rgba(16,185,129,.12)' if backend_online else 'rgba(239,68,68,.12)',
        'backend_icon': '🟢' if backend_online else '🔴',
    })
    context.update(nav_context('dashboard'))
    return render(request, 'admin/dashboard.html', context)


def analytics_view(request):
    total_sessions = ChatHistoryDB.objects.count()
    total_messages = sum(len(json.loads(s.messages)) if s.messages else 0 for s in ChatHistoryDB.objects.all())
    avg_msgs = round(total_messages / total_sessions, 1) if total_sessions else 0

    context = admin.site.each_context(request)
    context.update({
        'leaderboard': get_leaderboard(),
        'hourly_activity': json.dumps(get_hourly_activity()),
        'hourly_labels': json.dumps([f"{h}:00" for h in range(24)]),
        'avg_msgs': avg_msgs,
    })
    context.update(nav_context('analytics'))
    return render(request, 'admin/analytics.html', context)


def system_view(request):
    env_status = get_env_status()
    vs_info = get_vectorstore_info()
    context = admin.site.each_context(request)
    context.update({
        'doc_files': get_doc_files(),
        'vs_text': f"Active ({vs_info['size_kb']} KB)" if vs_info['exists'] else "Not built",
        'vs_color': '#10b981' if vs_info['exists'] else '#ef4444',
        'groq_text': 'Configured ✅' if env_status['GROQ_API_KEY'] else 'Missing ❌',
        'groq_color': '#10b981' if env_status['GROQ_API_KEY'] else '#ef4444',
        'tavily_text': 'Configured ✅' if env_status['TAVILY_API_KEY'] else 'Missing ❌',
        'tavily_color': '#10b981' if env_status['TAVILY_API_KEY'] else '#ef4444',
        'secret_text': 'Set ✅' if env_status['SECRET_KEY'] else 'Missing ❌',
        'secret_color': '#10b981' if env_status['SECRET_KEY'] else '#ef4444',
        'server_time': timezone.now(),
    })
    context.update(nav_context('system'))
    return render(request, 'admin/system.html', context)


def security_view(request):
    failed_24h = FailedLoginDB.objects.filter(timestamp__gte=timezone.now() - timedelta(hours=24)).count()
    context = admin.site.each_context(request)
    context.update({
        'flagged_sessions': get_flagged_sessions(),
        'recent_failed': FailedLoginDB.objects.order_by('-timestamp')[:10],
        'failed_24h': failed_24h,
        'failed_color': '#ef4444' if failed_24h > 0 else '#10b981',
    })
    context.update(nav_context('security'))
    return render(request, 'admin/security.html', context)


_original_get_urls = admin.site.get_urls

def get_urls():
    custom = [
        path('', admin.site.admin_view(dashboard_view), name='index'),
        path('analytics/', admin.site.admin_view(analytics_view), name='analytics'),
        path('system/', admin.site.admin_view(system_view), name='system'),
        path('security/', admin.site.admin_view(security_view), name='security'),
        path('backup-db/', admin.site.admin_view(backup_db_view), name='backup_db'),
        path('global-search/', admin.site.admin_view(global_search_view), name='global_search'),
        path('delete-doc/', admin.site.admin_view(delete_doc_view), name='delete_doc'),
    ]
    return custom + _original_get_urls()

admin.site.get_urls = get_urls