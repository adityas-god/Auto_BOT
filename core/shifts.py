"""
On-Call Shift & Duty Roster Evaluation Engine for GreyOrange OpsBot.
Determines active shifts (Morning, Afternoon, Night) based on the site's configured timezone.
"""

from core.utils import get_current_datetime


def get_active_shift(shifts_cfg=None, tz_name="Asia/Kolkata"):
    cfg = shifts_cfg or {}
    now = get_current_datetime(tz_name)
    current_min = now.hour * 60 + now.minute

    shifts = [
        ("Morning", cfg.get("morning_hours", "06:00-14:00"), cfg.get("morning_users", "")),
        ("Afternoon", cfg.get("afternoon_hours", "14:00-22:00"), cfg.get("afternoon_users", "")),
        ("Night", cfg.get("night_hours", "22:00-06:00"), cfg.get("night_users", ""))
    ]

    def _parse_time_minutes(t_str):
        try:
            parts = t_str.strip().split(":")
            return int(parts[0]) * 60 + int(parts[1])
        except Exception:
            return 0

    def _is_time_in_range(check_min, start_min, end_min):
        if start_min <= end_min:
            return start_min <= check_min < end_min
        return check_min >= start_min or check_min < end_min

    for name, hours, users in shifts:
        try:
            s_part, e_part = hours.split("-")
            s_min = _parse_time_minutes(s_part)
            e_min = _parse_time_minutes(e_part)
            if _is_time_in_range(current_min, s_min, e_min):
                u_list = [u.strip().strip("<>@") for u in (users or "").replace(",", " ").split() if u.strip()]
                return {
                    "name": name,
                    "hours": hours,
                    "users_raw": users or "",
                    "user_ids": u_list
                }
        except Exception:
            continue

    default_users = cfg.get("morning_users", "")
    u_list = [u.strip().strip("<>@") for u in (default_users or "").replace(",", " ").split() if u.strip()]
    return {
        "name": "General",
        "hours": "All-Day",
        "users_raw": default_users or "",
        "user_ids": u_list
    }
