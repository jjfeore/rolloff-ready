"""Validate untrusted input and compute conservative, explainable screening results."""

import json
import math
import re
from pathlib import Path

CATALOG = json.loads(Path(__file__).with_name("catalog.json").read_text(encoding="utf-8"))
CONTAINERS = {item["size"]: item for item in CATALOG["containers"]}


class Problem(Exception):
    def __init__(self, status, code, message):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def invalid(message):
    raise Problem(400, "INVALID_INPUT", message)


def obj(value, label):
    if not isinstance(value, dict):
        invalid(f"{label} must be an object.")
    return value


def text(value, label, maximum, minimum=1):
    if not isinstance(value, str):
        invalid(f"Enter {label}.")
    value = value.strip()
    if not minimum <= len(value) <= maximum or any(ord(c) < 32 and c not in "\n\t" for c in value):
        invalid(f"Check {label}; use {minimum}–{maximum} characters.")
    return value


def number(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not math.isfinite(value)):
        invalid(f"{label} must be a finite number.")
    if value < minimum or value > maximum:
        invalid(f"{label} is outside the supported range.")
    return float(value)


def us_coordinate(lat, lng):
    # Coarse coverage guard, not a country-boundary geofence. Country is checked
    # against HERE's result as well. Include Alaska, Hawaii, and US territories.
    return any(s <= lat <= n and w <= lng <= e for s, n, w, e in (
        (24, 50, -125, -66), (51, 72, -180, -129), (51, 72, 170, 180),
        (18, 29, -179, -154), (17, 19, -68, -64), (13, 21, 144, 146),
        (-15, -10, -172, -168),
    ))


def point(value, label):
    value = obj(value, label)
    lat = number(value.get("lat"), f"{label} latitude", -85, 85)
    lng = number(value.get("lng"), f"{label} longitude", -180, 180)
    if not us_coordinate(lat, lng):
        invalid("Choose a United States address.")
    return {"lat": lat, "lng": lng}


def placement(value):
    value = obj(value, "Placement")
    clean = point(value, "Placement")
    size = value.get("containerSize")
    if isinstance(size, bool) or not isinstance(size, int) or size not in CONTAINERS:
        invalid("Choose a 10, 15, 20, 30, or 40 yard container.")
    clean.update(containerSize=size, bearingDegrees=number(value.get("bearingDegrees"), "Rotation", 0, 360) % 360)
    return clean


def distance_m(a, b):
    p, q = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = q - p
    dlng = math.radians(b["lng"] - a["lng"])
    h = math.sin(dlat / 2) ** 2 + math.cos(p) * math.cos(q) * math.sin(dlng / 2) ** 2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1, h)))


def selected_address(value):
    raw_address = obj(value, "Address")
    if raw_address.get("countryCode") != "USA":
        invalid("Rolloff Ready currently supports United States addresses.")
    return {
        "id": text(raw_address.get("id"), "a selected address", 350),
        "label": text(raw_address.get("label"), "the street address", 500),
        "position": point(raw_address.get("position"), "Address"),
        "countryCode": "USA",
        "verification": text(raw_address.get("verification"), "a verified address selection", 160),
    }


def site_input(value):
    value = obj(value, "Site")
    address = selected_address(value.get("address"))
    spot = placement(value.get("placement"))
    if distance_m(address["position"], spot) > 250:
        invalid("Keep the container within 250 metres of the selected address.")
    return {"address": address, "placement": spot}


def assessment_input(value):
    site = site_input(value)
    raw = obj(value.get("answers"), "Site answers")
    clean = {}
    enums = {
        "space": ("yes", "no", "unsure"),
        "obstructions": ("no", "yes", "unsure"),
        "slope": ("level", "sideways", "inline", "unsure"),
        "differentPlane": ("no", "yes", "unsure"),
        "streetOverlap": ("no", "yes", "unsure"),
    }
    for key, allowed in enums.items():
        if raw.get(key) not in allowed:
            invalid("Answer every site question before continuing.")
        clean[key] = raw[key]
    if clean["slope"] == "inline":
        if raw.get("inlineDirection") not in ("uphill", "downhill", "unsure"):
            invalid("Tell us which way the drop site slopes from the truck.")
        clean["inlineDirection"] = raw["inlineDirection"]
    return {**site, "answers": clean}


def assess(data):
    answers = data["answers"]
    reasons = []
    if answers["space"] != "yes":
        reasons.append("The full truck and container space needs to be checked.")
    if answers["obstructions"] != "no":
        reasons.append("Obstructions or overhead clearance need an operator's review.")
    slope = answers["slope"]
    if slope == "sideways":
        reasons.append("A sideways slope can tilt the truck or container during unloading.")
    elif slope == "inline":
        direction = answers.get("inlineDirection")
        reasons.append({
            "uphill": "The container would roll uphill from the truck; unloading needs an operator's review.",
            "downhill": "The container would roll downhill from the truck; placement and control need review.",
            "unsure": "The direction of the drop-site slope needs to be checked.",
        }[direction])
    elif slope == "unsure":
        reasons.append("The drop-site slope has not been confirmed.")
    if answers["differentPlane"] != "no":
        reasons.append("The truck and container may stand on different planes or across a grade change.")
    if answers["streetOverlap"] != "no":
        reasons.append("Use of the street or sidewalk needs an access and permit check.")
    review = bool(reasons)
    return {
        "outcome": "review_needed" if review else "likely_suitable",
        "headline": "Let's take a closer look at your delivery." if review else "Your site looks promising.",
        "reasons": reasons if review else ["You confirmed clear space, a level drop site, and a shared level surface for the truck and container."],
        "summary": "An operator can review these details and help find a workable placement." if review else "Your answers suggest a straightforward placement. An operator will confirm access and equipment before delivery.",
    }


def customer(value):
    value = obj(value, "Contact details")
    if value.get("consent") is not True:
        invalid("Confirm that we may send your site and contact details with this request.")
    name = text(value.get("name"), "your name", 100)
    email = text(value.get("email"), "your email", 254)
    if not re.fullmatch(r"[^\s@<>\r\n]+@[^\s@<>\r\n]+\.[^\s@<>\r\n]+", email):
        invalid("Enter a valid email address.")
    phone = text(value.get("phone", ""), "your phone number", 35, 0)
    if phone and not re.fullmatch(r"[0-9+(). xX\-]{7,35}", phone):
        invalid("Check your phone number.")
    return {"name": name, "email": email, "phone": phone,
            "notes": text(value.get("notes", ""), "project notes", 2000, 0), "consent": True}


def email_content(data, person, result, request_id):
    p, a = data["placement"], data["answers"]
    box = CONTAINERS[p["containerSize"]]
    heading = "Delivery review and quote" if result["outcome"] == "review_needed" else "Quote request"
    labels = {"space": "Enough room for the full overlay", "obstructions": "Obstructions present",
              "slope": "Drop-site slope", "inlineDirection": "Slope from truck toward container",
              "differentPlane": "Truck and container on different planes", "streetOverlap": "Street or sidewalk overlap"}
    lines = ["ROLLOFF READY", heading, f"Request ID: {request_id}", "", "CONTACT",
             f"Name: {person['name']}", f"Email: {person['email']}", f"Phone: {person['phone'] or 'Not provided'}",
             "", "SITE", data["address"]["label"],
             f"Container: {p['containerSize']} cubic yards ({box['lengthFeet']} x {box['widthFeet']} x {box['heightFeet']} ft, representative)",
             f"Truck clearance: {CATALOG['truck']['lengthFeet']} x {box['widthFeet']} ft",
             f"Container center: {p['lat']:.7f}, {p['lng']:.7f}",
             f"Bearing from truck toward container far end: {p['bearingDegrees']:.1f} degrees clockwise from north",
             f"Map: https://www.google.com/maps/search/?api=1&query={p['lat']:.7f}%2C{p['lng']:.7f}",
             "", "SITE ANSWERS"]
    lines.extend(f"{labels[key]}: {answer}" for key, answer in a.items())
    lines.extend(terrain_summary(data.get("terrain")))
    lines.extend(["", "SCREENING RESULT", result["headline"], *result["reasons"], result["summary"],
                  "", "PROJECT NOTES", person["notes"] or "None", "",
                  "Customer consented to send these details. Screening is based on customer answers. USGS terrain estimates are advisory; satellite imagery and a representative footprint do not approve delivery.",
                  "No measured driveway slope, curb transition, swept turning path, load-bearing capacity, or overhead survey is included."])
    return f"Rolloff Ready | {heading} | {p['containerSize']} yd", "\n".join(lines)


def terrain_summary(terrain):
    """Only server-issued, verified terrain evidence reaches this formatter."""
    lines = ["", "USGS TERRAIN ESTIMATE (ADVISORY)"]
    if not terrain:
        return lines + ["No verified elevation estimate was attached. Use the customer's site answers."]
    lines.extend([f"Source: {terrain['source']}", terrain["message"]])
    if terrain["status"] != "available":
        return lines
    along, across = terrain["along"], terrain["across"]
    lines.extend([
        f"Dataset: {terrain['sourceName']}; source date: {terrain.get('sourceDate') or 'Not supplied'}",
        f"Elevation resolution: {terrain['resolutionMeters']:g} m; estimated at: {terrain['estimatedAt']}",
        f"Lengthwise: {along['percent']:+.1f}% ({along['direction']}) across {along['baselineMeters']:.2f} m, positive toward the container's far end",
        f"Side-to-side: {across['percent']:+.1f}% ({across['direction']}) across {across['baselineMeters']:.2f} m, positive toward the right when looking toward the container",
        f"Visual review cue: {terrain['thresholdPercent']:g}% absolute slope; this is not an equipment safety limit.",
        f"Side samples are {terrain['lateralOffsetMeters']:g} m beyond each edge at the combined footprint midpoint.",
    ])
    labels = {"containerEnd": "Container far end", "truckFront": "Truck cab end", "left": "Left side", "right": "Right side"}
    for key, label in labels.items():
        sample = terrain["samples"][key]
        lines.append(f"{label}: {sample['lat']:.7f}, {sample['lng']:.7f}; {sample['elevationMeters']:.2f} m elevation")
    return lines + ["Nearest-neighbor terrain samples are estimates, not surveyed equipment support points. Four points cannot resolve curbs, grade breaks, or different truck/container planes."]
