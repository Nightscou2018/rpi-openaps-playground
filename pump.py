"""
Interface for openaps device "pump"

TODO: Document exceptions
"""
from collections import deque
from datetime import timedelta
from dateutil import parser
from itertools import cycle
import json

from cachetools import lru_cache
from cachetools import ttl_cache

#
# Caching
#

# Scatter long-lived caches by 6-minute intervals so a 5-minute cycle doesn't end up with all misses
_cache_scatter_minutes = cycle(range(-49, 0, 7))


def _proxy_cache(from_func, to_func):
    """Proxies the cache management calls from a function generated by cachedfunc to another function

    :param from_func:
    :type from_func: function
    :param to_func:
    :type to_func: function
    """
    to_func.cache_info = from_func.cache_info
    to_func.cache_clear = from_func.cache_clear


class _CacheInfo():
    def __init__(self):
        pass

    def __call__(self):
        import inspect
        module = inspect.getmodule(self.__class__)
        members = inspect.getmembers(module, lambda value: hasattr(value, 'cache_info'))
        return {name: value.cache_info() for name, value in members if not name.startswith('_')}

cache_info = _CacheInfo()

#
# Pump data
#


@ttl_cache(ttl=24 * 60 * 60 + next(_cache_scatter_minutes))
def _carb_ratio_schedule():
    """

    :return:
    :rtype: list(dict(str, float))
    """
    return json.loads(_pump_output("read_carb_ratios"))["schedule"]


def carb_ratio_at_time(pump_time):
    """Returns the carb ratio at a given time of day on the pump clock

    Note that the parsing here is only applicable to MMX23 models and later.

    :param pump_time:
    :type pump_time: datetime.time
    :return:
    :rtype: float

    :raises: IndexError
    """
    pump_time_minutes = pump_time.hour * 60.0 + pump_time.minute + pump_time.second / 60.0
    carb_ratio_schedule = _carb_ratio_schedule()
    ratio = None

    for ratio_dict in carb_ratio_schedule:
        if pump_time_minutes < ratio_dict["offset"]:
            break
        else:
            ratio = ratio_dict["ratio"]

    if ratio is None:
        raise IndexError("No carb ratio found at time {}".format(pump_time_minutes))
    return ratio


_proxy_cache(_carb_ratio_schedule, carb_ratio_at_time)


def clock_datetime():
    """Returns the current date and time from the pump's system clock

    :return:
    :rtype: datetime.datetime
    """
    pump_datetime_iso = json.loads(_pump_output("read_clock"))
    return parser.parse(pump_datetime_iso)


@lru_cache(maxsize=1)
def _latest_glucose_entry_in_range(from_datetime, to_datetime):
    """Returns the latest glucose history entry in the specified range

    :param from_datetime:
    :type from_datetime: datetime.datetime
    :param to_datetime:
    :type to_datetime: datetime.datetime
    :return: A dictionary describing the glucose reading, or None if no glucose readings were found
    :rtype: dict|NoneType
    """
    glucose_pages_dict = json.loads(
        _pump_output(
            "filter_glucose_date",
            from_datetime.isoformat(),
            to_datetime.isoformat()
        )
    )
    last_page = glucose_pages_dict["end"]
    glucose_history = json.loads(_pump_output("read_glucose_data", str(last_page)))

    current_glucose_dict = next(
        (x for x in reversed(glucose_history) if x["name"] in ("GlucoseSensorData", "CalBGForGH")),
        {}
    )

    if from_datetime <= parser.parse(current_glucose_dict["date"]) <= to_datetime:
        return current_glucose_dict
    return None


def glucose_level_at_datetime(pump_datetime):
    """Returns the most-recent glucose level at a specified time in the sensor history

    Returns None if no glucose readings were recorded in the 15 minutes before `pump_datetime`

    :param pump_datetime:
    :type pump_datetime: datetime.datetime
    :return: The most-recent glucose level (mg/dL), or None
    :rtype: int|NoneType
    """
    # truncate the seconds to create a 60s ttl
    to_datetime = pump_datetime.replace(second=0, microsecond=0)
    from_datetime = to_datetime - timedelta(minutes=15)

    glucose_history_dict = _latest_glucose_entry_in_range(from_datetime, to_datetime) or {}

    return glucose_history_dict.get("sgv", glucose_history_dict.get("amount", None))


_proxy_cache(_latest_glucose_entry_in_range, glucose_level_at_datetime)


@lru_cache(maxsize=1)
def _history_in_range(from_datetime, to_datetime):
    next_page_num = 0
    last_datetime = to_datetime
    history_queue = deque()
    # Expect entries may be out-of-order up to this amount
    time_discrepancy = timedelta(minutes=5)
    results = []

    while from_datetime <= last_datetime + time_discrepancy:
        # If we're out of entries, get the next page
        if len(history_queue) == 0:
            history_queue.extend(json.loads(_pump_output("read_history_data", str(next_page_num))))
            next_page_num += 1
        entry = history_queue.popleft()
        try:
            last_datetime = parser.parse(entry.get("timestamp"))
        except (AttributeError, ValueError):
            pass
        entry["timestamp"] = last_datetime

        if last_datetime >= from_datetime:
            results.append(entry)

    return results


def history_in_range(from_datetime, to_datetime):
    # truncate the seconds to create a 60s ttl
    return _history_in_range(
        from_datetime.replace(second=0, microsecond=0),
        to_datetime.replace(second=0, microsecond=0)
    )

_proxy_cache(_history_in_range, history_in_range)


@ttl_cache(ttl=24 * 60 * 60 + next(_cache_scatter_minutes))
def insulin_action_curve():
    """

    :return:
    :rtype: int
    """
    settings_dict = json.loads(_pump_output("read_settings"))
    return settings_dict["insulin_action_curve"]


@ttl_cache(ttl=24 * 60 * 60 + next(_cache_scatter_minutes))
def _insulin_sensitivity_schedule():
    insulin_sensitivies_dict = json.loads(_pump_output("read_insulin_sensitivies"))

    return insulin_sensitivies_dict["sensitivities"]


def insulin_sensitivity_at_time(pump_time):
    """Returns the insulin sensitivity at a given time of day on the pump clock

    :param pump_time:
    :type pump_time: datetime.time
    :return:
    :rtype: int
    """

    # TODO: Support a sensitivity schedule
    return _insulin_sensitivity_schedule()[0]["sensitivity"]


_proxy_cache(_insulin_sensitivity_schedule, insulin_sensitivity_at_time)


def _pump_output(*args):
    """Executes an `openaps use` command against the `pump` device

    TODO: Expect `report` calls instead of `use` calls

    :param args:
    :type args: tuple(str)
    :return:
    :rtype: str

    :raises: CalledProcessError
    """
    from subprocess import check_output

    args_list = ["openaps", "use", "pump"]
    args_list.extend(args)

    return check_output(args_list)
