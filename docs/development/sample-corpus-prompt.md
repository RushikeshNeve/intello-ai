# Sample corpus prompt (for a ChatGPT agent with web browsing)

Purpose: build the test material for M04 (search and grounded chat: the golden evaluation set, `tests/evaluation/datasets/`)
and M06 (spreadsheets, code, images, audio). Needed before **M04 evaluation work**; it can be prepared any time.

How to use: paste the prompt below into a ChatGPT agent. Tip: run it once with "Pilot: do only 5 documents and 10
questions" first and check the format, then run the full version. Put the downloaded files and the two output files in
`D:\Career\Projects\Universal Knowlege Hub\sample-corpus\` (git-ignored; keep it out of the repo) and tell me.

---

```text
You are building a small, legally safe test corpus for a document question-answering system. Use web search and browsing.

1. Collect 30-40 documents (hard limit 50). Only openly licensed or public-domain material, nothing with personal data.
Choose 3 topic clusters (for example science/health, technology/open source, public policy/economics) with at least 8
documents each, so some questions need several documents. Use stable direct-download links. Mix:
- ~12 PDFs (open-access papers, government or NGO reports, 5-60 pages)
- ~8 Markdown or plain-text files (READMEs, project docs, public-domain essays)
- ~6 spreadsheets/CSVs from open-data portals, each with a short data dictionary
- 3 tiny code repos (MIT/Apache/BSD, under 5 MB, give the exact commit SHA)
- optional: 3 images of text and 2 public-domain audio clips under 3 minutes
Each file at most 20 MB, total at most 250 MB. Skip a document rather than guess its license.

2. Output A, manifest.csv: id, filename, direct_url, source_page, license, type, cluster, pages_or_rows, why_chosen.
If you can create files, also give me a ZIP; otherwise I will download from the URLs.

3. Output B, questions.jsonl, 120-150 questions, one JSON object per line:
{"id","question","type","expected_answer","evidence":[{"file","location","quote"}],"difficulty"}
Types: ~60% single_doc (answer is in one place), ~20% multi_doc (needs 2+ documents), ~10% unanswerable (plausible but
NOT in the corpus; expected_answer "not answerable"; empty evidence), ~5% numeric_table (needs a spreadsheet cell or
table), ~5% trap (the question rests on a wrong or outdated premise; the answer explains why).
"location" = page number, section heading, sheet + cell range, or file:line. "quote" = at most 20 words, copied exactly.
Mix easy, medium and hard. Write questions the way a real user would; never say "the document".

4. Open the source for every answerable question and confirm the quote and location. List anything you could not verify.
Never invent sources or quotes.
```
