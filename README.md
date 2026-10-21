# workshop-reviewer-forms

Find the Google Forms that upcoming ML/NLP/CV workshops use to recruit reviewers, all in one list. Only forms that are still accepting responses are listed.

**Just want the list?** Open the page, which is rebuilt every day: **https://workshop-reviewer-forms.vercel.app**

Most workshops post their "call for reviewers" form on their own website rather than on OpenReview. This script uses OpenReview only to list the workshops, then reads each workshop's website for the form.

## Run it yourself

Needs Python 3.9+. It uses only the standard library, so there is nothing to install.

```
git clone https://github.com/Xiaoyu68/workshop-reviewer-forms.git
cd workshop-reviewer-forms
python3 find_forms.py
```

When the run finishes, the page opens in your browser. A full run takes 5 to 10 minutes, mostly waiting on OpenReview's rate limit.

| file | contents |
|---|---|
| `site/index.html` | the page: search, filter by conference, sort by workshop date, "new" badges. It works when opened straight from disk |
| `site/data.json` | the same data as JSON |
| `state/report.csv` | every form found, with the text around it, so you can check the labelling |
| `state/seen.json` | forms seen so far; later runs mark forms not in it as **New** |

Options:

```
python3 find_forms.py --no-open    # don't open the browser
python3 find_forms.py --all        # also keep non-reviewer forms (registration, mentorship, ...)
python3 find_forms.py --limit 20   # quick test on 20 workshops; writes to state/test/ and leaves the real results alone
python3 find_forms.py --no-notify  # skip the macOS notification
python3 find_forms.py --publish ./deploy.sh   # redeploy the page every minute while crawling, and at the end
```

`deploy.sh` deploys `site/` to Vercel. It reads `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID` from the environment or from a git-ignored `.vercel.env` file.

## How it works

1. **Discover.** It reads every workshop venue on OpenReview: the `active_venues` group, plus `<conference>/<this year and next>/Workshop/` for every conference that has workshops. Nothing has to be updated by hand each season.
2. **Filter.** It keeps workshops that have not taken place yet. The paper deadline is read from OpenReview and shown for reference only. Many workshops keep recruiting reviewers after it, so it does not decide what is listed.
3. **Crawl.** It fetches each website once: the homepage, plus up to 6 same-site pages whose link looks reviewer-related (call for reviewers, PC, committee, join, ...).
4. **Extract, label and check.** It pulls out every `forms.gle` and `docs.google.com/forms` link. A link counts as a reviewer form when its own link text asks for reviewers, PC members or area chairs, for example "Sign up as a reviewer" or "Join PC". When the link text is generic, such as "here", the sentence before it decides. Each reviewer form is then opened, and forms that Google shows as closed ("no longer accepting responses") or deleted are dropped. An open form is the closest thing to "the reviewer call has not closed yet".
5. **Diff.** It compares the results with earlier runs, so new forms are marked. On macOS, new forms also trigger a desktop notification.

## Keep it updated

**Your own hosted copy (Vercel).** `.github/workflows/update.yml` crawls every day at 13:00 UTC and deploys `site/` to Vercel. You can also start it by hand from the Actions tab. To set it up on a fork:

1. Run `vercel link` in the repo once. It creates `.vercel/project.json`, which holds `orgId` and `projectId`.
2. Create a token at https://vercel.com/account/tokens.
3. Add three repository secrets: `VERCEL_TOKEN`, `VERCEL_ORG_ID` and `VERCEL_PROJECT_ID`.
4. Optionally add a repository variable `SITE_URL`, set to your deployment URL. With it, "new" badges carry over between runs.

**Daily on your Mac:**

```
./install_launchd.sh        # every day at 09:00
./install_launchd.sh 8      # every day at 08:00
./install_launchd.sh --uninstall
```

**Linux:** use cron: `0 9 * * * cd /path/to/workshop-reviewer-forms && python3 find_forms.py --no-notify --no-open`.

## Configuration

`config.json`:

- `extra_prefixes`: more OpenReview venue prefixes to scan, if a conference is missed (for example `ICML.cc/2027/Workshop/`).
- `subpage_keywords`: a regex that decides which links from a homepage get followed.
- `reviewer_anchor`: a regex applied to a link's own text to decide whether it is a reviewer form.
- `reviewer_keywords`: a regex applied to the sentence before a link whose text is generic.
- `max_subpages`, `context_chars`, `workers`: crawl limits.

## Limits

- **OpenReview only.** Workshops that use only CMT, EasyChair or SoftConf are missed.
- **"Open" means the form still accepts responses.** Some organizers never close their form after they have enough reviewers, so an open form is not a guarantee they still need you.
- **Static HTML only.** A form link that is injected by JavaScript is missed.
- **Off-site posts are missed.** Calls posted only on X, Slack or mailing lists are out of scope.
- **The labelling is keyword-based.** Check `report.csv`, or expand "Text around the link" on the page.

Each run sends a few hundred GET requests, one per workshop site plus a few subpages. Please don't run it more than a few times a day.

## License

MIT
