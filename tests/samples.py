"""
Synthetic job-alert emails for V2 tests — PII-free, modeled on the real HTML structure of each sender
(sampled 2026-09). Real alert emails are never committed: they contain the recipient's name and
LinkedIn login tokens in their URLs.

  LINKEDIN   — title is the link; company + location sit in the next table cell
  GLASSDOOR  — one link holds "Company rating ★ Title Location $salary"; stable jobListingId
  MONSTER    — title link + "QUICK APPLY" + "VIEW JOB", all one-time tracking redirects
  LENNYS     — title links straight to the employer's job board; company is its own link after
  NEWSLETTER — articles only, no postings
"""

from __future__ import annotations

LINKEDIN_HTML = """<html><head><title>Jobs</title><style>.x{color:red}</style></head><body>
<table>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/search-results/?keywords=fde&amp;trk=1">Your job alert for forward deployed engineer</a></td></tr>
 <tr><td>Duane, apply today&#847;&#847;&#847;&#847;</td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/view/4457198691/?trackingId=abc&amp;otpToken=SECRET">Forward Deployed Engineer</a></td></tr>
 <tr><td>Acme Robotics · San Francisco, CA (Hybrid)</td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/view/4465501084/?trackingId=def">Senior Solutions Architect</a></td></tr>
 <tr><td>Globex Corporation · Remote</td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/search-results/?more=1">See all jobs</a></td></tr>
 <tr><td><a href="https://www.linkedin.com/comm/jobs/alerts">Manage alerts</a> · <a href="https://www.linkedin.com/unsub">Unsubscribe</a></td></tr>
</table><script>track("<a href='https://evil.example/x'>fake</a>")</script></body></html>"""

GLASSDOOR_HTML = """<html><body><div>
<a href="https://www.glassdoor.com/Job/san-mateo-jobs.htm">search for more jobs.</a>
<table><tr><td><a href="https://www.glassdoor.com/partner/jobListing.htm?pos=101&amp;jobListingId=1009876543&amp;ao=1">
  <div>Notable Health</div><div>3.0 ★</div><div>Engineering Manager</div><div>San Mateo, CA</div><div>$220K</div></a></td></tr>
<tr><td><a href="https://www.glassdoor.com/partner/jobListing.htm?pos=102&amp;jobListingId=1001112223">
  <div>Hex</div><div>4.0 ★</div><div>AI Research Engineer</div><div>San Francisco, CA</div></a></td></tr>
</table>
<a href="https://www.glassdoor.com/about/privacy.htm">Privacy Policy</a>
</div></body></html>"""

MONSTER_HTML = """<html><body><table>
<tr><td><a href="http://click.monster.com/f/a/AAA">Forward Deployed Engineer</a></td></tr>
<tr><td>Initech · Austin, TX</td></tr>
<tr><td><a href="http://click.monster.com/f/a/BBB">QUICK APPLY</a> <a href="http://click.monster.com/f/a/CCC">VIEW JOB</a></td></tr>
<tr><td><a href="http://click.monster.com/f/a/DDD">Platform Engineer</a></td></tr>
<tr><td>Umbrella Labs · Remote</td></tr>
<tr><td><a href="http://click.monster.com/f/a/EEE">QUICK APPLY</a> <a href="http://click.monster.com/f/a/FFF">VIEW JOB</a></td></tr>
</table></body></html>"""

LENNYS_HTML = """<html><body>
<p><a href="https://www.lennysjobs.com/myjobs?utm_source=trueup">461 new jobs</a></p>
<p><a href="https://jobs.ashbyhq.com/snowflake/639bb38f-0c50-4a1b?utm_source=trueup">Sr Manager, Applied Field Engineering</a>
 at <a href="https://www.trueup.io/co/snowflake?utm_source=trueup">Snowflake</a>
 <a href="https://www.trueup.io/co/snowflake/jobs">300+ open jobs</a></p>
<p><a href="https://job-boards.greenhouse.io/reddit/jobs/8022345?gh_src=x">Engineering Manager, Ads ML</a>
 at <a href="https://www.trueup.io/co/reddit">Reddit</a></p>
</body></html>"""

NEWSLETTER_HTML = """<html><body>
<h1>Editors' Picks</h1>
<p><a href="https://ltc8.awstrack.me/L0/https://builtin.com/articles/1">Turns Out 'Stop Hiring Humans' Was Terrible Advice</a></p>
<p><a href="https://ltc8.awstrack.me/L0/https://builtin.com/articles/2">Read Article</a></p>
</body></html>"""

SENDERS = {
    "linkedin": "LinkedIn Job Alerts <jobalerts-noreply@linkedin.com>",
    "glassdoor": "Glassdoor Jobs <noreply@glassdoor.com>",
    "monster": "Monster <monster@notifications.monster.com>",
    "lennys": "Lenny's Jobs <lennysjobs@trueup.io>",
    "builtin": "Built In <support@builtin.com>",
}


def auth_pass(from_domain: str, authserv: str = "mail.protonmail.ch") -> list[str]:
    """Authentication-Results as Proton writes them (one result per header, top-down)."""
    return [
        f"{authserv}; dmarc=pass (p=reject dis=none) header.from={from_domain}",
        f"{authserv}; spf=pass smtp.mailfrom=bounce.{from_domain}",
        f"{authserv}; dkim=pass (2048-bit key) header.d={from_domain}",
    ]


def make_email(html: str = "", *, sender: str = SENDERS["linkedin"], subject: str = "Job alert",
               message_id: str = "<m1@x>", auth: list[str] | None = None,
               text: str = "") -> dict:
    from agent.senders import sender_domain
    return {
        "message_id": message_id, "subject": subject, "sender": sender, "received_at": None,
        "body_text": text, "body_html": html,
        "auth_results": auth if auth is not None else auth_pass(sender_domain(sender)),
        "has_attachments": False,
    }


def linkedin_picks():
    """A correct LinkPicks answer for LINKEDIN_HTML."""
    from agent.extract import extract_links
    from agent.schemas import LinkPicks, PickedPosting
    ids = {c.text: c.id for c in extract_links(LINKEDIN_HTML)}
    return LinkPicks(postings=[
        PickedPosting(link_id=ids["Forward Deployed Engineer"], title="Forward Deployed Engineer",
                      company="Acme Robotics"),
        PickedPosting(link_id=ids["Senior Solutions Architect"], title="Senior Solutions Architect",
                      company="Globex Corporation"),
    ])
