# workshop-reviewer-forms

Find the Google Forms that upcoming ML/NLP/CV workshops use to recruit reviewers, all in one list, and get told when a new one appears.

Most workshops post their "call for reviewers" form on their own website rather than on OpenReview. This script uses OpenReview only to list the workshops, then reads each workshop's website for the form.

## Quick start

Needs Python 3.9+. It uses only the standard library, so there is nothing to install.

```
git clone https://github.com/Xiaoyu68/workshop-reviewer-forms.git
cd workshop-reviewer-forms
python3 find_forms.py
```

A full run takes about a minute. It writes:

| file | contents |
|---|---|
| `state/report.md` | reviewer forms, sorted by workshop date |
| `state/report.csv` | every form found, with its label and the text around it, so you can check the labelling |
| `state/seen.json` | forms seen so far; later runs mark forms not in it as 🆕 |

Options:

```
python3 find_forms.py --all        # also list non-reviewer forms (registration, mentorship, ...)
python3 find_forms.py --limit 20   # crawl only 20 workshops, for a quick test
python3 find_forms.py --no-notify  # skip the macOS notification
```

## How it works

1. **Discover.** It reads every workshop venue from the public OpenReview API: the `active_venues` group, plus the prefixes in `config.json`. Each venue gives a website, the workshop date and the paper deadline.
2. **Filter.** It keeps workshops that have not happened yet. If a venue has no date, it uses the paper deadline or the venue's creation date instead.
3. **Crawl.** It fetches each website once: the homepage, plus up to 6 same-site pages whose link looks reviewer-related (call for reviewers, PC, committee, join, ...).
4. **Extract and label.** It pulls out every `forms.gle` and `docs.google.com/forms` link. A link is labelled `reviewer` when the text around it mentions reviewers, reviewing or a program committee.
5. **Diff.** It compares the results with `state/seen.json`. New reviewer forms are marked 🆕 and, on macOS, trigger a desktop notification.

## Run it daily (macOS)

```
./install_launchd.sh        # every day at 09:00
./install_launchd.sh 8      # every day at 08:00
./install_launchd.sh --uninstall
```

On Linux, use cron instead: `0 9 * * * cd /path/to/workshop-reviewer-forms && python3 find_forms.py --no-notify`.

## Configuration

`config.json`:

- `openreview_prefixes`: extra venue prefixes to scan, for example `ICML.cc/2027/Workshop/`. `active_venues` already covers most open venues, so the prefixes are a safety net. Update them each season.
- `subpage_keywords`: a regex that decides which links from a homepage get followed.
- `reviewer_keywords`: a regex that decides whether a form counts as a reviewer form.
- `max_subpages`, `context_chars`, `workers`: crawl limits.

## Limits

- **OpenReview only.** Workshops that use only CMT, EasyChair or SoftConf are missed. Most ML workshops use OpenReview, but many NLP and robotics workshops do not.
- **Static HTML only.** A form link that is injected by JavaScript is missed.
- **Off-site posts are missed.** Calls posted only on X, Slack or mailing lists are out of scope.
- **The labelling is keyword-based.** Check `report.csv` if a form looks wrong.

The script sends a few hundred polite GET requests per run, one per workshop site plus a few subpages. Please don't run it more than a few times a day.

## License

MIT
