"""The synthetic world (WP2 world model).

Organizations, people, owners, venues and events are authored fixtures; the
world composes them into coherent entities and selects a sender/signer/host for
each scenario. Every rendered message references only world entities, every
mailbox is on its own entity's domain, and scenario objects are drawn from the
selected org's declared catalog. Selection fails closed: a family with no
eligible industry+role entity raises rather than silently substituting one.
"""
import datetime
import os
import re

from . import identity, recipes, temporal
from .errors import BuildError
from .rng import stream

WORLD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "world")
# Social events may fall on a weekend; business deadlines stay on business days.
_SOCIAL_EVENT_FAMILIES = ("personal_invitation", "event_registration")


def _load(name):
    return recipes.load_world(name)


class World(object):
    def __init__(self, orgs, people, owners, contacts, colleagues, places,
                 family_world, config):
        self.config = config
        self.tld = (config.get("tld_profiles") or {}).get(
            config.get("tld_profile", "reserved"), "example")
        self.family_world = family_world
        self.places_all = places
        self.places = places.get("venues", {})
        self.events = places.get("events", {})
        self.orgs = orgs
        self.org_by_id = {o["id"]: o for o in orgs}
        self.people = people
        self.people_by_org = {}
        for person in people:
            self.people_by_org.setdefault(person["org_id"], []).append(person)
        self.owners = {o["persona"]: o for o in owners}
        self.contacts = contacts
        self.colleagues = colleagues
        self.weekdays = tuple((config.get("business") or {}).get(
            "weekdays", [0, 1, 2, 3, 4]))
        self.domains = set(o["domain"] for o in orgs)
        self.domains.update(o["domain"] for o in owners)
        self.domains.update(c["domain"] for c in contacts)

    # -- construction ------------------------------------------------------
    @classmethod
    def build(cls):
        config = _load("config.json")
        tld = (config.get("tld_profiles") or {}).get(
            config.get("tld_profile", "reserved"), "example")
        orgs = []
        for raw in _load("orgs.json")["orgs"]:
            org = dict(raw)
            org["slug"] = identity.org_slug(org["name"])
            org["domain"] = identity.org_domain(org["name"], tld)
            catalog = {}
            for bucket, terms in (org.get("catalog") or {}).items():
                catalog[bucket] = [re.sub(r"^the\s+", "", t) for t in terms]
            org["catalog"] = catalog
            orgs.append(org)
        names = _load("names.json")
        people = []
        for org in orgs:
            pool = names["given"].get(org["region"], names["given"]["us"])
            fam_pool = names["family"].get(org["region"], names["family"]["us"])
            depts = org.get("departments") or ["Office"]
            for i in range(min(3, max(2, len(org.get("role_mailboxes") or {})))):
                given = pool[(len(org["id"]) + i) % len(pool)]
                family = fam_pool[(len(org["id"]) * 2 + i) % len(fam_pool)]
                role = depts[i % len(depts)]
                people.append({
                    "id": "%s-%d" % (org["id"], i + 1), "given": given,
                    "family": family, "full": identity.display_name(given, family),
                    "org_id": org["id"], "org": org["name"], "role": role,
                    "region": org["region"], "domain": org["domain"],
                    "email": identity.person_email(given, family, org["domain"]),
                    "signature_lines": [identity.display_name(given, family),
                                        "%s, %s" % (role, org["name"])],
                })
        owners = []
        for raw in _load("owners.json")["owners"]:
            owner = dict(raw)
            owner["domain"] = "%s.%s" % (owner["domain_slug"], tld)
            owner["name"] = identity.display_name(owner["given"], owner["family"])
            owner["email"] = identity.person_email(owner["given"], owner["family"],
                                                   owner["domain"])
            owner["team_alias"] = "team@%s" % owner["domain"]
            owners.append(owner)
        # Real colleagues of each owner: same org/domain as the owner, so a
        # colleague sender is a genuine member of the owner's organisation.
        colleagues = {}
        for owner in owners:
            pool = names["given"].get(owner["region"], names["given"]["us"])
            fam_pool = names["family"].get(owner["region"], names["family"]["us"])
            team = []
            for i in range(2):
                given = pool[(len(owner["persona"]) + i + 3) % len(pool)]
                family = fam_pool[(len(owner["persona"]) * 2 + i + 5) % len(fam_pool)]
                team.append({
                    "id": "%s-colleague-%d" % (owner["persona"], i + 1),
                    "given": given, "family": family,
                    "full": identity.display_name(given, family),
                    "org_id": owner["persona"], "org": owner["org"],
                    "domain": owner["domain"], "role": "Colleague",
                    "region": owner["region"],
                    "email": identity.person_email(given, family, owner["domain"]),
                    "signature_lines": [identity.display_name(given, family),
                                        owner["org"]],
                })
            colleagues[owner["persona"]] = team
        contacts = []
        cpool = names["contacts"]
        for i in range(len(cpool["given"])):
            given = cpool["given"][i]
            family = cpool["family"][i % len(cpool["family"])]
            domain = "%s.%s" % (identity.slugify("%s-%s" % (given, family)), tld)
            contacts.append({
                "id": "contact-%d" % (i + 1), "given": given, "family": family,
                "full": identity.display_name(given, family), "domain": domain,
                "org": "%s %s" % (given, family),
                "email": identity.person_email(given, family, domain),
                "region": "uk", "role": "Friend",
                "signature_lines": [identity.display_name(given, family)],
            })
        places = _load("places.json")
        world = cls(orgs, people, owners, contacts, colleagues, places,
                    _load("family_world.json")["family_world"], config)
        return world

    # -- lookups -----------------------------------------------------------
    def owner(self, persona):
        raw = self.owners.get(persona["persona"])
        if raw is None:
            raise BuildError("no world owner for persona %r" % persona["persona"])
        return raw

    def eligible_orgs(self, industries, roles, buckets=None, allowed=None,
                      exclude=None):
        """Orgs that match an industry, hold a required role and carry a
        compatible catalog bucket.

        Fails closed: there is no 'matching or anything' fallback, so an
        ineligible industry/role/object combination raises instead of silently
        borrowing an unrelated org or an unrelated object.
        """
        wanted_industry = set(industries or [])
        wanted_roles = set(roles or [])
        wanted_buckets = set(buckets or [])
        cands = list(self.orgs)
        if allowed is not None:
            cands = [o for o in cands if o["id"] in allowed]
        elif exclude:
            cands = [o for o in cands if o["id"] not in exclude]

        def ok(org):
            if org.get("industry") not in wanted_industry:
                return False
            if not (wanted_roles & set((org.get("role_mailboxes") or {}).keys())):
                return False
            if wanted_buckets and not (wanted_buckets & set((org.get("catalog") or {}).keys())):
                return False
            return True

        cands = [o for o in cands if ok(o)]
        if not cands:
            raise BuildError(
                "no eligible org for industries %s / roles %s / objects %s "
                "(allowed=%s)" % (sorted(wanted_industry), sorted(wanted_roles),
                                  sorted(wanted_buckets),
                                  sorted(allowed) if allowed else None))
        return cands

    def people_for_org(self, org_id):
        return self.people_by_org.get(org_id) or list(self.people)

    # -- selection ---------------------------------------------------------
    def _org_sender(self, family_id, spec, stream, allowed=None, exclude=None):
        org = stream.pick(self.eligible_orgs(spec.get("industries"),
                                             spec.get("roles"),
                                             buckets=spec.get("object"),
                                             allowed=allowed, exclude=exclude))
        mailboxes = org.get("role_mailboxes") or {}
        role = next(r for r in spec.get("roles") or [] if r in mailboxes)
        localpart = mailboxes[role]
        person = stream.pick(self.people_for_org(org["id"]))
        return {
            "kind": "org", "org": org["name"], "org_id": org["id"],
            "domain": org["domain"], "email": identity.role_email(localpart, org["domain"]),
            "role": role, "org_ref": org,
            "person": person, "person_name": person["full"],
            "person_email": identity.person_email(person["given"], person["family"],
                                                  org["domain"]),
            "signature_lines": list(person.get("signature_lines") or [person["full"]]),
        }

    def _person_sender(self, spec, owner, stream):
        context = spec.get("context", "friend")
        if context == "colleague":
            person = stream.pick(self.colleagues[owner["persona"]])
            return {
                "kind": "person", "org": owner["org"],
                "org_id": owner["persona"], "domain": owner["domain"],
                "email": person["email"], "role": "Colleague",
                "region": owner.get("region", "uk"), "person": person,
                "person_name": person["full"], "person_email": person["email"],
                "signature_lines": list(person["signature_lines"]),
            }
        person = stream.pick(self.contacts)
        return {
            "kind": "person", "org": person["org"], "org_id": person["id"],
            "domain": person["domain"], "email": person["email"], "role": "Friend",
            "region": person.get("region", "uk"), "person": person,
            "person_name": person["full"], "person_email": person["email"],
            "signature_lines": list(person["signature_lines"]),
        }

    def select_sender(self, family_id, owner, stream, allowed=None, exclude=None):
        spec = self.family_world.get(family_id)
        if spec is None:
            raise BuildError("no world mapping for family %r" % family_id)
        if spec.get("kind") == "person":
            return self._person_sender(spec, owner, stream), spec
        return self._org_sender(family_id, spec, stream, allowed=allowed,
                                exclude=exclude), spec

    def catalog_object(self, sender, spec, stream):
        """Pick an object from the sender org's declared catalog, or None.

        The object bucket is the family's declared ``object`` (goods/services/
        documents/projects/courses); a person sender has no catalog. Returns
        ``(bucket, term)`` or ``(None, None)``.
        """
        if sender.get("kind") != "org":
            return None, None
        catalog = (sender.get("org_ref") or {}).get("catalog") or {}
        for bucket in spec.get("object") or []:
            terms = catalog.get(bucket) or []
            if terms:
                return bucket, stream.pick(terms)
        return None, None

    def venue(self, region_key, stream):
        return stream.pick(self.places.get(region_key, self.places["uk"]))

    def city(self, region_key, stream):
        cities = (self.places_all or {}).get("cities") or {}
        return stream.pick(cities.get(region_key, cities.get("uk", ["London"])))

    def event(self, region_key, event_date, stream):
        """A named event whose season matches the actual held date."""
        season = temporal.season_of(event_date, {"region": region_key})
        pool = [e for e in self.events.get(region_key, self.events["uk"])
                if e["season"] == season]
        if not pool:
            raise BuildError("no %s event authored for region %r"
                             % (season, region_key))
        return stream.pick(pool)

    # -- scenario composition ---------------------------------------------
    def _region_key(self, sender, owner):
        if sender.get("kind") == "org":
            return (sender.get("org_ref") or {}).get("region",
                                                     owner.get("region", "uk"))
        return sender.get("region", owner.get("region", "uk"))

    @staticmethod
    def _region_dict(region_key):
        for region in recipes.regions():
            if region["region"] == region_key:
                return region
        return recipes.regions()[0]

    def _send_datetime(self, stream):
        horizon = self.config.get("horizon") or {}
        start = temporal.parse(horizon.get("start", "2025-01-06")).replace(
            tzinfo=datetime.timezone.utc)
        days = int(horizon.get("days", 330))
        hours = list((self.config.get("business") or {}).get(
            "send_hours", [9, 10, 14, 16]))
        dt = start.replace(hour=stream.pick(hours),
                          minute=stream.pick([0, 15, 30, 45]), second=0,
                          microsecond=0)
        dt = dt + datetime.timedelta(days=stream.randint(days))
        while dt.weekday() not in self.weekdays:
            dt = dt + datetime.timedelta(days=1)
        return dt

    def _dates(self, family_id, send, stream):
        spec = temporal.WINDOWS.get(family_id, {})

        def rel(pair, base=None):
            base = base or send
            lo, hi = pair
            return temporal.add_business_days(base, lo + stream.randint(hi - lo + 1),
                                              self.weekdays)

        dates = {}
        if "txn" in spec:
            lo, hi = spec["txn"]
            dates["txn"] = temporal.add_business_days(
                send, lo + stream.randint(hi - lo + 1), self.weekdays)
        if "overdue" in spec:
            lo, hi = spec["overdue"]
            dates["overdue"] = temporal.add_business_days(
                send, -(lo + stream.randint(hi - lo + 1)), self.weekdays)
        for key in ("due", "until", "arrival", "meeting", "checkpoint", "milestone"):
            if key in spec:
                dates[key] = rel(spec[key])
        if "event" in spec:
            lo, hi = spec["event"]
            event = send + datetime.timedelta(days=lo + stream.randint(hi - lo + 1))
            if family_id in _SOCIAL_EVENT_FAMILIES and event.weekday() < 5:
                event = event + datetime.timedelta(days=5 - event.weekday())
            dates["event"] = event
            if "rsvp" in spec:
                lo, hi = spec["rsvp"]
                rsvp = temporal.add_business_days(
                    event, -(lo + stream.randint(hi - lo + 1)), self.weekdays)
                if rsvp <= send:
                    rsvp = temporal.add_business_days(send, 2, self.weekdays)
                dates["rsvp"] = rsvp
        if "register_by" in spec:
            lo, hi = spec["register_by"]
            rb = temporal.add_business_days(
                dates.get("event", send), -(lo + stream.randint(hi - lo + 1)),
                self.weekdays)
            if rb <= send:
                rb = temporal.add_business_days(send, 2, self.weekdays)
            dates["register_by"] = rb
        return dates

    def build_scenario(self, family_id, persona, index, seed, domain, shift_axis):
        """Compose world-coherent slots + lint facts for one triage root."""
        rng = stream(seed, "domain:%s:scenario:%d" % (domain, index))
        owner = self.owner(persona)
        style_orgs = set(self.config.get("style_shift_org_ids") or [])
        allowed = None
        exclude = set(style_orgs)
        if shift_axis == "source_style_shift":
            allowed, exclude = set(style_orgs), set()
        sender, spec = self.select_sender(family_id, owner, rng, allowed=allowed,
                                          exclude=exclude)
        region_key = self._region_key(sender, owner)
        region = self._region_dict(region_key)
        send = self._send_datetime(rng)
        dates = self._dates(family_id, send, rng)
        v = recipes.vocab()

        def amount(pool):
            return region["currency"] + rng.pick(v[pool])

        if "event" in dates:
            primary = "event"
            secondary = (dates.get("rsvp") or dates.get("register_by")
                         or temporal.add_business_days(dates["event"], 7,
                                                       self.weekdays))
        else:
            primary = None
            for key in ("due", "until", "meeting", "arrival", "checkpoint",
                        "milestone"):
                if key in dates:
                    primary = key
                    break
            if primary is None:
                dates["due"] = temporal.add_business_days(send, 5, self.weekdays)
                primary = "due"
            secondary = dates.get("milestone")
            if secondary is None:
                secondary = temporal.add_business_days(dates[primary], 7,
                                                       self.weekdays)

        bucket, term = self.catalog_object(sender, spec, rng)
        event = None
        event_season = None
        if "event" in dates or family_id in ("event_registration", "school_community"):
            event = self.event(region_key, dates.get("event", send), rng)
            event_season = event["season"]
        if family_id == "security_notification":
            place = self.city(region_key, rng)
        else:
            place = self.venue(region_key, rng)
        code = "%s%d" % (rng.pick(v["code_prefix"]), 100 + index)
        vendor2 = rng.pick(self.orgs)
        vendor3 = rng.pick(self.orgs)
        deadline_soon = temporal.add_business_days(send, 3, self.weekdays)
        deadline_late = temporal.add_business_days(send, 25, self.weekdays)
        deadline_past = temporal.add_business_days(send, -5, self.weekdays)

        # Bind the family's object to the sender's catalog where the family
        # references an item/service/document/project; fall back to a neutral
        # generic only for person senders or families with no catalog bucket.
        catalog = (sender.get("org_ref") or {}).get("catalog") or {}
        item = term or rng.pick(v["item"])
        if bucket == "services":
            service = term
        elif catalog.get("services"):
            service = rng.pick(catalog["services"])
        else:
            service = rng.pick(v["service"])
        if bucket == "courses":
            course = term
        elif catalog.get("courses"):
            course = rng.pick(catalog["courses"])
        else:
            course = rng.pick(v["course"])

        slots = {
            "owner_first": owner["given"], "owner_addr": owner["email"],
            "signer": sender["person_name"], "vendor": sender["org"],
            "vendor2": vendor2["name"], "vendor3": vendor3["name"],
            "vendor_domain": sender["domain"],
            "vendor2_domain": vendor2["domain"], "vendor3_domain": vendor3["domain"],
            "vendor_slug": sender["domain"].split(".")[0],
            "vendor2_slug": vendor2["domain"].split(".")[0],
            "vendor3_slug": vendor3["domain"].split(".")[0],
            "org": sender["org"], "org_slug": identity.slugify(sender["org"]),
            "news_vendor": sender["org"], "news_slug": identity.slugify(sender["org"]),
            "item": item, "service": service,
            "detail": rng.pick(v["detail_neutral"] if spec.get("detail_style") == "neutral"
                               else v["detail"]),
            "place": place, "person": sender["person"]["given"], "course": course,
            "event_name": event["name"] if event else rng.pick(v["event_name"]),
            "month": temporal.MONTH_NAMES[send.month - 1],
            "amount": amount("amount_values"), "amount2": amount("amount2_values"),
            "amount_late": amount("amount_values"), "amount_soon": amount("amount_values"),
            "amount_paid": amount("amount_values"), "percent": rng.pick(v["percent"]),
            "code": code, "link": "https://%s/%s" % (sender["domain"], code.lower()),
            "ref1": "%s-%04d" % (rng.pick(v["ref_prefixes"]), 100 + index),
            "ref2": "%s-%04d" % (rng.pick(v["ref_prefixes"]), 200 + index),
            "ref3": "%s-%04d" % (rng.pick(v["ref_prefixes"]), 300 + index),
            "ref": "%s-%04d" % (rng.pick(v["ref_prefixes"]), 100 + index),
            "ticket": "%s-%04d" % (v["ticket_prefix"], 100 + index),
            "date": temporal.format_date(dates.get("txn", send), region),
            "deadline": temporal.format_deadline(dates[primary], region),
            "deadline2": temporal.format_deadline(secondary, region),
            "deadline_soon": temporal.format_deadline(deadline_soon, region),
            "deadline_late": temporal.format_deadline(deadline_late, region),
            "deadline_past": temporal.format_deadline(deadline_past, region),
            "hour": send.strftime("%H:%M"), "sender": sender["person_name"],
            "sender_slug": sender["email"].split("@")[0],
            "sender_domain": sender["domain"],
            "sender_subject": "Can you reply about %s?" % item,
        }
        fam = recipes.families().get(family_id) or {}
        blobs = [str(t.get("body") or "") + str(t.get("subject") or "")
                 for t in fam.get("templates") or []]
        for key in ("paraphrase", "resolved"):
            variant = fam.get(key)
            if isinstance(variant, dict):
                blobs.append(str(variant.get("body") or ""))
        signature_expected = bool(blobs) and all("{signer}" in b for b in blobs)
        item_expected = bool(blobs) and all("{item}" in b for b in blobs)
        service_expected = bool(blobs) and all("{service}" in b for b in blobs)

        facts = {
            "family": family_id, "send": send.isoformat(), "region": region_key,
            "style_profile": "shift" if shift_axis == "source_style_shift"
            else "standard",
            "relationship": spec.get("relationship"),
            "catalog_bucket": bucket, "catalog_item": term,
            "signature_expected": signature_expected,
            "item_expected": item_expected, "service_expected": service_expected,
            "sender": {"kind": sender["kind"], "org": sender["org"],
                       "org_id": sender.get("org_id"), "domain": sender["domain"],
                       "email": sender["email"], "person_name": sender["person_name"],
                       "role": sender["role"]},
            "signer": {"name": sender["person_name"], "email": sender["email"],
                       "domain": sender["domain"], "org": sender["org"],
                       "signature_lines": list(sender.get("signature_lines") or
                                              [sender["person_name"]])},
            "recipient": {"name": owner["name"], "email": owner["email"],
                          "domain": owner["domain"], "org": owner["org"],
                          "team_alias": owner.get("team_alias")},
        }
        for key, dt in dates.items():
            facts[key] = dt.isoformat()
        facts["deadline_soon"] = deadline_soon.isoformat()
        facts["deadline_late"] = deadline_late.isoformat()
        facts["deadline_past"] = deadline_past.isoformat()
        facts["deadline2"] = secondary.isoformat()
        if event is not None:
            facts["event_name"] = event["name"]
            facts["event_season"] = event_season
            facts["venue"] = place
        if family_id in ("personal_invitation", "school_community",
                         "event_registration", "meeting_request"):
            facts["host"] = {"person": sender["person_name"]}
        return slots, facts, region, facts["style_profile"]
