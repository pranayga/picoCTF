"""Module for interacting with the achievements."""

from datetime import datetime
from importlib.machinery import SourceFileLoader
from os.path import join

import pymongo
from voluptuous import Range, Required, Schema

import api.config
import api.db
import api.user
from api.annotations import log_action
from api.common import (check, InternalException, safe_fail,
                        validate, WebException)
from api.models import Achievement, AchievementSchema

achievement_schema = Schema({
    Required("name"):
    check(("The achievement's display name must be a string.", [str])),
    Required("score"):
    check(("Score must be a positive integer.", [int, Range(min=0)])),
    Required("event"):
    check(("Type must be a string.", [str])),
    Required("description"):
    check(("The description must be a string.", [str])),
    Required("processor"):
    check(("The processor path must be a string.", [str])),
    Required("hidden"):
    check(("An achievement's hidden state is either True or False.",
           [lambda hidden: type(hidden) == bool])),
    Required("image"):
    check(("An achievement's image path must be a string.", [str])),
    Required("smallimage"):
    check(("An achievement's smallimage path must be a string.", [str])),
    "disabled":
    check(("An achievement's disabled state is either True or False.",
           [lambda disabled: type(disabled) == bool])),
    "multiple":
    check(("Whether an achievement can be earned multiple times" +
           "is either True or False.",
           [lambda disabled: type(disabled) == bool])),
    "aid":
    check(("You should not specify a aid for an achievement.",
           [lambda _: False])),
    "_id":
    check(("Your achievements should not already have _ids.",
           [lambda id: False]))
})


def _get_achievement(aid, show_disabled=False):
    """
    Get a single achievement by aid.

    Args:
        aid: the achievement id
        show_disabled: Boolean indicating whether or not to
                       show disabled achievements.
    """
    match = {'aid': aid}

    if not show_disabled:
        match.update({"disabled": False})

    db = api.db.get_conn()
    achievement = db.achievements.find_one(match, {"_id": 0})

    if achievement is None:
        raise InternalException("Could not find achievement! DB query: {}"
                                .format(str(match)))
    return achievement


def _get_all_achievements(event=None, show_disabled=False):
    """
    Get all of the achievements in the database.

    Args:
        event: Optional parameter to restrict which achievements are returned
        show_disabled: Boolean indicating whether or not to
                       show disabled achievements.
    Returns:
        List of achievements from the database

    """
    db = api.db.get_conn()

    match = {}
    if event is not None:
        match.update({'event': event})

    if not show_disabled:
        match.update({'disabled': False})

    return list(
        db.achievements.find(match, {"_id": 0}).sort(
            'score', pymongo.ASCENDING))


def get_earned_achievement_instances(tid=None, uid=None, aid=None):
    """
    Get the solved achievements for a given team or user.

    Args:
        tid: The team id
        event: Optional parameter to restrict which achievements are returned
    Returns:
        List of solved achievements
    """
    db = api.db.get_conn()

    match = {}

    if uid is not None:
        match.update({"uid": uid})
    elif tid is not None:
        match.update({"tid": tid})

    if aid is not None:
        match.update({"aid": aid})

    return list(db.earned_achievements.find(match, {"_id": 0}))


def get_earned_aids(tid=None, uid=None, aid=None):
    """
    Get the solved aids for a given team or user.

    Args:
        tid: The team id
        event: Optional parameter to restrict which achievements are returned
    Returns:
        List of solved achievement ids
    """
    return set([
        a["aid"]
        for a in get_earned_achievement_instances(tid=tid, uid=uid, aid=aid)
    ])


def set_earned_achievements_seen(tid=None, uid=None):
    """
    Set all earned achievements from a team or user seen.

    Args:
        tid: the team id
        uid: the user id
    """
    db = api.db.get_conn()

    match = {}

    if tid is not None:
        match.update({"tid": tid})
    elif uid is not None:
        match.update({"uid": uid})
    else:
        raise InternalException("You must specify either a tid or uid")

    db.earned_achievements.update(match, {"$set": {"seen": True}}, multi=True)


def get_earned_achievements_display(tid=None, uid=None):
    """
    Get the achievement display for a given user/team.

    Includes instance specific information.

    Args:
        tid: The team id
        tid: The user id
    Returns:
        A list of enabled achievements the team has earned.
    """
    instance_achievements = get_earned_achievement_instances(tid=tid, uid=uid)
    set_earned_achievements_seen(tid=tid, uid=uid)

    for instance_achievement in instance_achievements:
        achievement = _get_achievement(instance_achievement["aid"])

        # Make sure not to override name or description.
        achievement.pop("name")
        achievement.pop("description")

        instance_achievement.update(achievement)

        # Make sure to remove sensitive data
        instance_achievement.pop("data", None)

    return instance_achievements


def get_earned_achievements(tid=None, uid=None):
    """
    Get the solved achievements for a given team or user.

    Args:
        tid: The team id
        tid: The user id
    Returns:
        List of solved achievement dictionaries
    """
    achievements = get_earned_achievement_instances(tid=tid, uid=uid)
    set_earned_achievements_seen(tid=tid, uid=uid)

    for achievement in achievements:
        achievement.update(_get_achievement(achievement["aid"]))
        achievement.pop("data")

    return achievements


def set_achievement_disabled(aid, disabled):
    """
    Update a achievement's availability.

    Args:
        aid: the achievement's aid
        disabled: whether or not the achievement should be disabled.
    Returns:
        The updated achievement object.

    """
    return update_achievement(aid, {"disabled": disabled})


def get_processor(aid):
    """
    Return the processor module for a given achievement.

    Args:
        aid: the achievement id
    Returns:
        The processor module

    """
    try:
        path = _get_achievement(aid, show_disabled=True)["processor"]
        base_path = api.config.get_settings(
        )["achievements"]["processor_base_path"]
        return SourceFileLoader(path[:-3], join(base_path, path)).load_module()
    except FileNotFoundError:
        raise InternalException("Achievement processor is offline.")


@log_action
def process_achievement(aid, data):
    """
    Determine whether or not an achievement has been earned.

    Should not be called directly.

    Args:
        aid: the achievement id
        data: additional data dictionary
    """
    if data.get("uid", None) is None:
        data["uid"] = api.user.get_user()["uid"]

    if data.get("tid", None) is None:
        data["tid"] = api.user.get_user(uid=data["uid"])["tid"]

    _get_achievement(aid=aid, show_disabled=True)
    processor = get_processor(aid)

    return processor.process(api, data)


def insert_earned_achievement(aid, data):
    """
    Store earned achievement for a user/team.

    Args:
        aid: the achievement id
        data: the data necessary to assess the achievement
              must include tid, uid
    """
    db = api.db.get_conn()

    tid, uid = data.pop("tid"), data.pop("uid")
    name, description = data.pop("name"), data.pop("description")

    db.earned_achievements.insert({
        "aid": aid,
        "tid": tid,
        "uid": uid,
        "data": data,
        "name": name,
        "description": description,
        "timestamp": datetime.utcnow().timestamp(),
        "seen": False
    })


def process_achievements(event, data):
    """
    Process achievements of a type with data.

    Args:
        event: event type, e.g., submit
        data: dictionary with additional information necessary for assessment
    """
    if data.get("uid", None) is None:
        data["uid"] = api.user.get_user()["uid"]

    if data.get("tid", None) is None:
        data["tid"] = api.user.get_user(uid=data["uid"])["tid"]

    eligible_achievements = [
        achievement for achievement in _get_all_achievements(event=event)
        if achievement["aid"] not in get_earned_aids(
            tid=data["tid"]) or achievement.get("multiple", False)
    ]

    for achievement in eligible_achievements:
        aid = achievement["aid"]

        acquired, instance_info = process_achievement(aid, data)

        info = {
            "name": achievement.get("name"),
            "description": achievement.get("description")
        }

        info.update(instance_info)
        data.update(info)
        if acquired:
            insert_earned_achievement(aid, data)


def insert_achievement(achievement):
    """
    Insert an achievement object into the database.

    Args:
        achievement(str): the achievement definition as JSON.
    Raises:
        WebException: an achievement with the same name already exists,
                      or insertion into the database failed

    """
    db = api.db.get_conn()
    validate(achievement_schema, achievement)

    achievement["disabled"] = achievement.get("disabled", False)

    achievement["aid"] = api.common.hash(achievement["name"])

    if safe_fail(get_achievement, aid=achievement["aid"]) is not None:
        raise WebException("achievement with identical aid already exists.")

    if safe_fail(get_achievement, name=achievement["name"]) is not None:
        raise WebException("achievement with identical name already exists.")

    db.achievements.insert(achievement)

    return achievement["aid"]


def update_achievement(aid, updated_achievement):
    """
    Update a achievement with new properties.

    Args:
        aid: the aid of the achievement to update.
        updated_achievement: an updated achievement object.
    Returns:
        The updated achievement object.

    """
    db = api.db.get_conn()

    if updated_achievement.get("name", None) is not None:
        if safe_fail(
                get_achievement, name=updated_achievement["name"]) is not None:
            raise WebException(
                "Achievement with identical name already exists.")

    achievement = _get_achievement(aid, show_disabled=True).copy()
    achievement.update(updated_achievement)

    # pass validation by removing/re-adding aid
    achievement.pop("aid")
    validate(achievement_schema, achievement)
    achievement["aid"] = aid

    db.achievements.update({"aid": aid}, achievement)

    return achievement
