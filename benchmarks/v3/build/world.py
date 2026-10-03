"""The synthetic world (WP2 world model).

Organizations, people, owners, venues and events are authored fixtures; the
world composes them into coherent entities and selects a sender/signer/host for
each scenario. Every rendered message references only world entities, and every
mailbox is on its own entity's domain -- this is the substrate that makes the
U3/U4/U5 identity defects impossible.
"""
import datetime
import os

from . import identity, recipes, temporal
from .errors import BuildError
from .rng import stream

WORLD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "fixtures", "world")


def _load(name):
    return recipes.load_world(name)


class World(object):
    def __init__(self, orgs, people, owners, contacts, places, family_world,
                 config):
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
                person_id = "%s-%d" % (org["id"], i + 1)
                people.append({
                    "id": person_id, "given": given, "family": family,
                    "full": identity.display_name(given, family),
                    "org_id": org["id"], "org": org["name"],
                    "role": role, "region": org["region"],
                    "domain": org["domain"],
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
            owners.append(owner)
        contacts = []
        cpool = names["contacts"]
        for i in range(len(cpool["given"])):
            given = cpool["given"][i]
            family = cpool["family"][i % len(cpool["family"])]
            domain = "%s.%s" % (identity.slugify("%s-%s" % (given, family)), tld)
            contacts.append({
                "id": "contact-%d" % (i + 1), "given": given, "family": family,
                "full": identity.display_name(given, family),
                "domain": domain, "org": "%s %s" % (given, family),
                "email": identity.person_email(given, family, domain),
                "region": "uk", "role": "Friend",
                "signature_lines": [identity.display_name(given, family)],
            })
        places = _load("places.json")
        world = cls(orgs, people, owners, contacts, places,
                    _load("family_world.json")["family_world"], config)
        return world

    # -- lookups -----------------------------------------------------------
    def owner(self, persona):
        raw = self.owners.get(persona["persona"])
        if raw is None:
            raise BuildError("no world owner for persona %r" % persona["persona"])
        return raw

    def orgs_for_industries(self, industries, allowed=None, exclude=None):
        wanted = set(industries or [])
        base = list(self.orgs)
        if allowed is not None:
            restricted = [o for o in base if o["id"] in allowed]
            if restricted:
                base = restricted
        elif exclude:
            pruned = [o for o in base if o["id"] not in exclude]
            if pruned:
                base = pruned
        matching = [o for o in base if o.get("industry") in wanted]
        return matching or base

    def people_for_org(self, org_id):
        return self.people_by_org.get(org_id) or list(self.people)

    # -- selection ---------------------------------------------------------
    def _org_sender(self, family_id, spec, stream, allowed=None, exclude=None):
        org = stream.pick(self.orgs_for_industries(spec.get("industries"),
                                                   allowed=allowed, exclude=exclude))
        role = spec.get("role")
        mailboxes = org.get("role_mailboxes") or {}
        if role and role in mailboxes:
            localpart = mailboxes[role]
        elif mailboxes:
            role = stream.pick(sorted(mailboxes))
            localpart = mailboxes[role]
        else:
            role, localpart = "support", "support"
        person = stream.pick(self.people_for_org(org["id"]))
        email = identity.role_email(localpart, org["domain"])
        return {
            "kind": "org", "org": org["name"], "org_id": org["id"],
            "domain": org["domain"], "email": email, "role": role,
            "person": person, "person_name": person["full"],
            "person_email": identity.person_email(person["given"], person["family"],
                                                 org["domain"]),
            "signature_lines": list(person.get("signature_lines") or [person["full"]]),
        }

    def _person_sender(self, spec, owner, stream):
        context = spec.get("context", "friend")
        if context == "colleague":
            given_pool = [p for p in self.people if p["org_id"] in
                          [o["id"] for o in self.orgs]]
            person = stream.pick(given_pool)
            domain = owner["domain"]
            email = identity.person_email(person["given"], person["family"], domain)
            return {
                "kind": "person", "org": owner["org"], "org_id": "owner",
                "domain": domain, "email": email, "role": "Colleague",
                "region": owner.get("region", "uk"),
                "person": person, "person_name": person["full"],
                "person_email": email,
                "signature_lines": [person["full"], owner["org"]],
            }
        person = stream.pick(self.contacts)
        return {
            "kind": "person", "org": person["org"], "org_id": person["id"],
            "domain": person["domain"], "email": person["email"], "role": "Friend",
            "region": person.get("region", "uk"),
            "person": person, "person_name": person["full"],
            "person_email": person["email"],
            "signature_lines": list(person["signature_lines"]),
        }

    def select_sender(self, family_id, owner, stream, allowed=None, exclude=None):
        spec = self.family_world.get(family_id)
        if spec is None:
            raise BuildError("no world mapping for family %r" % family_id)
        if spec.get("kind") == "person":
            return self._person_sender(spec, owner, stream)
        return self._org_sender(family_id, spec, stream, allowed=allowed,
                                exclude=exclude)

    def venue(self, region_key, stream):
        return stream.pick(self.places.get(region_key, self.places["uk"]))

    def city(self, region_key, stream):
        cities = (self.places_all or {}).get("cities") or {}
        return stream.pick(cities.get(region_key, cities.get("uk", ["London"])))

    def event(self, region_key, dt, stream):
        region = {"region": region_key}
        season = temporal.season_of(dt, region)
        pool = [e for e in self.events.get(region_key, self.events["uk"])
                if e["season"] == season]
        if not pool:
            pool = self.events.get(region_key, self.events["uk"])
        return stream.pick(pool)

    def contact_for_owner(self, owner, stream):
        return stream.pick(self.contacts)

    # -- scenario composition ---------------------------------------------
    def _region_key(self, sender, owner):
        if sender.get("kind") == "org":
            return self.org_by_id.get(sender.get("org_id"), {}).get(
                "region", owner.get("region", "uk"))
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
        dt = start.replace(hour=stream.pick(hours), minute=stream.pick([0, 15, 30, 45]),
                          second=0, microsecond=0)
        dt = dt + datetime.timedelta(days=stream.randint(days))
        while dt.weekday() not in self.weekdays:
            dt = dt + datetime.timedelta(days=1)
        return dt

    def _dates(self, family_id, send, stream):
        spec = temporal.WINDOWS.get(family_id, {})

        def rel(pair, base=None):
            base = base or send
            lo, hi = pair
            n = lo + stream.randint(hi - lo + 1)
            return temporal.add_business_days(base, n, self.weekdays)

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
            event = rel(spec["event"])
            dates["event"] = event
            if "rsvp" in spec:
                lo, hi = spec["rsvp"]
                n = lo + stream.randint(hi - lo + 1)
                rsvp = temporal.add_business_days(event, -n, self.weekdays)
                if rsvp <= send:
                    rsvp = temporal.add_business_days(send, 2, self.weekdays)
                dates["rsvp"] = rsvp
        if "register_by" in spec:
            dates["register_by"] = rel(spec["register_by"])
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
        sender = self.select_sender(family_id, owner, rng, allowed=allowed,
                                    exclude=exclude)
        region_key = self._region_key(sender, owner)
        region = self._region_dict(region_key)
        send = self._send_datetime(rng)
        dates = self._dates(family_id, send, rng)
        v = recipes.vocab()

        def amount(pool):
            return region["currency"] + rng.pick(v[pool])

        if "event" in dates:
            # ``{deadline}`` is the event date and ``{deadline2}`` the rsvp /
            # registration reply-by, matching the family templates.
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

        event = None
        if "event" in dates or family_id in ("event_registration", "school_community"):
            event = self.event(region_key, send, rng)
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

        slots = {
            "owner_first": owner["given"],
            "owner_addr": owner["email"],
            "signer": sender["person_name"],
            "vendor": sender["org"],
            "vendor2": vendor2["name"], "vendor3": vendor3["name"],
            "vendor_domain": sender["domain"],
            "vendor2_domain": vendor2["domain"], "vendor3_domain": vendor3["domain"],
            "vendor_slug": sender["domain"].split(".")[0],
            "vendor2_slug": vendor2["domain"].split(".")[0],
            "vendor3_slug": vendor3["domain"].split(".")[0],
            "org": sender["org"], "org_slug": identity.slugify(sender["org"]),
            "news_vendor": sender["org"],
            "news_slug": identity.slugify(sender["org"]),
            "item": rng.pick(v["item"]), "service": rng.pick(v["service"]),
            "detail": rng.pick(v["detail"]),
            "place": place,
            "person": sender["person"]["given"],
            "course": rng.pick(v["course"]),
            "event_name": event["name"] if event else rng.pick(v["event_name"]),
            "month": temporal.MONTH_NAMES[send.month - 1],
            "amount": amount("amount_values"),
            "amount2": amount("amount2_values"),
            "amount_late": amount("amount_values"),
            "amount_soon": amount("amount_values"),
            "amount_paid": amount("amount_values"),
            "percent": rng.pick(v["percent"]),
            "code": code,
            "link": "https://%s/%s" % (sender["domain"], code.lower()),
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
            "hour": send.strftime("%H:%M"),
            "sender": sender["person_name"],
            "sender_slug": sender["email"].split("@")[0],
            "sender_domain": sender["domain"],
            "sender_subject": "Can you reply about %s?" % rng.pick(v["item"]),
        }
        facts = {
            "family": family_id, "send": send.isoformat(),
            "style_profile": "shift" if shift_axis == "source_style_shift"
            else "standard",
            "sender": {"kind": sender["kind"], "org": sender["org"],
                       "org_id": sender.get("org_id"),
                       "domain": sender["domain"], "email": sender["email"],
                       "person_name": sender["person_name"], "role": sender["role"]},
            "signer": {"name": sender["person_name"], "email": sender["email"],
                       "domain": sender["domain"], "org": sender["org"],
                       "signature_lines": list(sender.get("signature_lines") or
                                              [sender["person_name"]])},
            "recipient": {"name": owner["name"], "email": owner["email"],
                          "domain": owner["domain"], "org": owner["org"]},
        }
        for key, dt in dates.items():
            facts[key] = dt.isoformat()
        facts["deadline_soon"] = deadline_soon.isoformat()
        facts["deadline_late"] = deadline_late.isoformat()
        facts["deadline_past"] = deadline_past.isoformat()
        facts["deadline2"] = secondary.isoformat()
        if event is not None:
            facts["event_name"] = event["name"]
            facts["venue"] = place
        if family_id in ("personal_invitation", "school_community",
                         "event_registration", "meeting_request"):
            facts["host"] = {"person": sender["person_name"]}
        fam = recipes.families().get(family_id) or {}
        blobs = [str(t.get("body") or "") + str(t.get("subject") or "")
                 for t in fam.get("templates") or []]
        for key in ("paraphrase", "resolved"):
            variant = fam.get(key)
            if isinstance(variant, dict):
                blobs.append(str(variant.get("body") or ""))
        facts["signature_expected"] = any("{signer}" in b for b in blobs)
        return slots, facts, region, facts["style_profile"]

