from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Cron:
    def __init__(self, expression, timezone_name):
        if not isinstance(expression, str) or len(expression.split()) != 5:
            raise ValueError("cron 需要五个字段：分 时 日 月 周")
        try:
            self.zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError, TypeError):
            raise ValueError("无效的时区，例如 Asia/Shanghai") from None
        fields = expression.split()
        bounds = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
        self.values = [self.parse_field(field, *bound) for field, bound in zip(fields, bounds)]
        self.values[4] = {value % 7 for value in self.values[4]}
        self.day_star = fields[2].startswith("*")
        self.week_star = fields[4].startswith("*")

    @staticmethod
    def parse_field(field, minimum, maximum):
        values = set()
        try:
            for item in field.split(","):
                base, *steps = item.split("/")
                if len(steps) > 1:
                    raise ValueError()
                step = int(steps[0]) if steps else 1
                if step < 1:
                    raise ValueError()
                if base == "*":
                    start, end = minimum, maximum
                elif "-" in base:
                    start, end = map(int, base.split("-"))
                else:
                    start = int(base)
                    end = maximum if steps else start
                if not minimum <= start <= end <= maximum:
                    raise ValueError()
                values.update(range(start, end + 1, step))
        except ValueError:
            raise ValueError(f"无效的 cron 字段：{field}") from None
        return values

    def next_after(self, timestamp):
        minutes, hours, days, months, weekdays = self.values
        first_day = datetime.fromtimestamp(timestamp, self.zone).date()
        for offset in range(366 * 8):
            day = first_day + timedelta(days=offset)
            if day.month not in months:
                continue
            day_matches = day.day in days
            week_matches = (day.weekday() + 1) % 7 in weekdays
            matches = (day_matches and week_matches) if self.day_star or self.week_star else (day_matches or week_matches)
            if not matches:
                continue
            candidates = set()
            for hour in sorted(hours):
                for minute in sorted(minutes):
                    local = datetime(day.year, day.month, day.day, hour, minute)
                    for fold in (0, 1):
                        candidate = local.replace(tzinfo=self.zone, fold=fold).astimezone(timezone.utc)
                        if candidate.astimezone(self.zone).replace(tzinfo=None) != local:
                            continue
                        value = candidate.timestamp()
                        if value > timestamp:
                            candidates.add(value)
            if candidates:
                return min(candidates)
        raise ValueError("cron 在未来八年内没有可执行时间")
