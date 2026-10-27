# Writing a BenchPanel plugin with Claude

A guide to using **Claude Code** (in your terminal) or **Claude.ai** (in the browser)
to write a plugin for an instrument BenchPanel doesn't support yet.

Claude can read a 300-page programming manual, write the plugin files and fix
them until they pass the checker far faster than doing it by hand. It can also be
confidently wrong about an instrument it has never seen. This guide is about getting
the first without the second. Every rule in it comes from something that actually
went wrong while the plugins in `catalog/` were written.

Read with: [PLUGINS.md](PLUGINS.md) (the how-to) and
[PLUGIN_STANDARD.md](PLUGIN_STANDARD.md) (the contract a plugin must meet).

---

## Which one to use

| | Claude Code | Claude.ai |
|---|---|---|
| Works in | your terminal, inside the BenchPanel folder | a browser chat |
| Reads the repo | yes, by itself | only files you upload |
| Runs the checker | yes, and fixes what fails | no: you run it and paste the output back |
| Talks to your instrument | yes, through scripts it writes and runs (you approve each one) | no: you run its scripts and paste the results back |
| Best for | the whole job, start to finish | when you can't or won't run Claude Code on the bench machine |

Both end in the same place: a plugin folder that passes `tools/check_plugin.py` and
has been tried on the real instrument.

---

## Before you start

You need all of these. Collect them first.

1. **The right programming manual, for your exact model.** This matters more than
   anything else here. Sister instruments share a family look and different command
   sets. The 53181A plugin was nearly written from the *53131A/132A* guide, because
   that is the file that was labelled "53131/53181". Check the title page: model
   number, and the serial-number prefix it covers.
2. **The instrument, powered, connected, and with a known address:** its GPIB
   address, IP address or VISA resource string.
3. **BenchPanel, running**, with `pip install -r requirements.txt` done.
4. **Its current settings written down**, or a front-panel photo. You'll want to put
   them back afterwards.

---

## The rules to hold Claude to

Give Claude these. With Claude Code, the repo's `CLAUDE.md` already does. With
Claude.ai, paste them in (see the starter prompt below).

1. **Every SCPI command comes from the manual, with a page number.** Not from memory,
   and not from a similar model's manual. If the manual doesn't say, the answer is
   "the manual doesn't say", not a guess.
2. **Check the manual's page citations.** Claude will sometimes quote the right text
   and attach the page from a different document. In the 53181A driver, two of six
   page references came from the wrong guide. Ask it to verify each one.
3. **Nothing that can block the bus.** Many instruments hold the bus until a
   measurement completes: counters with no signal, DMMs on a slow trigger. The
   53181A's `READ?` with no signal wedged the connection until a device clear. The
   driver must *start* a measurement, *ask* whether it has finished (a status or event
   register that never blocks), and only then fetch.
4. **Read-only first.** Before any driver exists, Claude writes a probe that only
   *queries* settings (`*IDN?`, `FUNC?`, `...?`), never measures, and hands the front
   panel back. You run it and see what your instrument actually says.
5. **Outputs need your say-so.** Nothing that sources power or signal gets switched on
   without you agreeing first, and the plugin's output-on actions must be declared
   `"confirm"` in the manifest.
6. **Record, then restore.** Anything the driver changes on connect (run mode,
   trigger source) it records first and puts back in `on_release()`.
7. **Passing the checker is necessary, not sufficient.** The checker proves the plugin
   is *safe and well-formed*. Only the real instrument proves the commands are
   *right*.
8. **Evidence, not claims.** "It works" means readings you can see, compared with a
   known source, with the instrument's error queue empty. Ask for the numbers.

---

## With Claude Code

Open a terminal in the BenchPanel folder and start `claude`. It reads `CLAUDE.md`
automatically.

### 1. Hand it the manual and a model to copy

> I want a BenchPanel plugin for the **[make model]**. The programming guide is at
> `~/manuals/[file].pdf`. First confirm from its title page that it covers this model
> and my serial prefix **[prefix]**. Then read PLUGIN_STANDARD.md, PLUGINS.md, and
> the catalog plugin most like this instrument, and tell me which one you'll follow and
> why. Don't write any files yet.

Pick the closest example:
- no outputs, simple readings: `catalog/34401a`
- a supply or anything with an output: `catalog/e3641a`
- two channels, saved states: `catalog/33522a`
- measurements that can wait forever: `catalog/53181a`

### 2. Read-only probe

> Write a read-only probe script for my [model] at **[address]**. It should only
> send queries (no measurements, no READ?/MEAS?/FETC?, nothing that changes a
> setting), print each reply with its timing, and hand the front panel back at the
> end. Show me every command it will send before you run it.

Look at what it's about to send, and let it run. Keep the output: it's the
instrument's real starting state, and the first thing to put back later.

### 3. The measurement design, before any code

> From the manual, how do I start a measurement, find out it has finished *without
> blocking*, and fetch it? Quote the manual and give page numbers. What happens if
> there's no signal / no trigger?

If the answer doesn't name a register or event that never blocks, stop and push on it.
This is where most counter and meter drivers go wrong.

### 4. Write it, and make it pass

> Write the plugin in `catalog/[id]/`: manifest.json, driver.py, card.html, card.js,
> README.md. Every command cited to a page. Then run
> `python3 tools/check_plugin.py catalog/[id]` and fix it until it passes with no
> MUST failures. Show me the final checker output.

### 5. Try it with no hardware

> Run the panel with a scratch site file and plugins directory, start
> `tools/sim_scpi.py --model [MODEL]`, add it as a VISA resource, and check the card
> loads and the power button works.

The simulator proves the wiring, not the commands.

### 6. Test on the real instrument

> Plan a test on the real instrument. Tell me how to wire it, what known source
> you'll compare against, what you expect to see, and how you'll restore the settings
> from step 2. Don't switch any output on until I say so.

Wire it as asked, say go, and read the results. You want:
- readings compared with a known source, with the error stated;
- the error queue empty;
- the unhappy case tried (no signal, over-range, instrument switched off);
- a second instrument on the same bus still working throughout;
- afterwards, the settings read back from the instrument, not from the panel, and
  matching step 2.

### 7. Finish

> Record what we tested in the manifest's tested_on and in README.md, bump the version,
> and run the checker and tests/test_conformance.py again.

Then install it from the dashboard: **Instruments → Get plugins**. It's in the catalog
list, or zip the folder and use **Check this .zip**.

---

## With Claude.ai

Claude.ai can't run anything on your machine, so **you are its hands**. It writes,
you run, you paste back.

1. **Start a chat, or better a Project, so the files stay with you across
   conversations.** Upload:
   - the programming manual PDF;
   - `PLUGIN_STANDARD.md` and `PLUGINS.md`;
   - the four files of the closest catalog plugin (see step 1 above);
   - `benchcore/instrument.py`, so it sees the base class.
2. **Paste the starter prompt** below.
3. **Probe:** ask for the read-only probe script. Read it, run it, and paste the output
   back.
4. **Design:** ask the step 3 question above. Insist on page numbers.
5. **Write:** ask for the five files, one per code block. Save them into
   `catalog/[id]/`.
6. **Check:** run `python3 tools/check_plugin.py catalog/[id]` and paste the *whole*
   output back. Repeat until PASS.
7. **Test:** ask for a test plan, then run it through the dashboard (or its scripts) and
   paste the numbers back. Same pass criteria as step 6 above.
8. **Finish:** ask it to fill in `tested_on` and the README from the results you pasted.

If the chat is long, it may lose track of early details. Re-upload the manifest and
driver when you come back to it, rather than trusting it to remember them.

---

## Starter prompt

For Claude.ai, or for Claude Code if you want to be explicit:

```
I'm writing a BenchPanel instrument plugin for the [MAKE MODEL]
(serial prefix [PREFIX]), connected by [GPIB via a LAN gateway / LAN / USB].

Attached: its programming guide, PLUGIN_STANDARD.md, PLUGINS.md, the
[closest] catalog plugin as a model, and benchcore/instrument.py.

Rules:
1. Every SCPI command must come from the attached guide, with the page
   number. If the guide doesn't say, say so. Do not use other models' manuals.
2. First confirm the guide's title page covers my model and serial prefix.
3. Nothing may block the bus: start a measurement, check completion with a
   register or event that never blocks, only then fetch. Handle "no signal".
4. First give me a READ-ONLY probe script (queries only, no measurements),
   then wait for its output before designing anything.
5. Any action that can switch an output on must be declared "confirm" in
   the manifest and refuse without "confirmed": true.
6. Record anything on_connect changes and restore it in on_release.
7. The plugin must pass tools/check_plugin.py with no MUST failures.
8. Don't tell me it works; tell me what was measured and against what.

Start with rule 2, then the probe.
```

---

## Reviewing what Claude wrote

Before it goes near the instrument, read these yourself. None take long.

| Look at | You want to see |
|---|---|
| `driver.py` docstring | the manual's title and part number, and page numbers next to the commands |
| every `query(...)` | nothing that waits for a measurement unless it has already been told one finished |
| `manifest.json` → `actions` | every action that can turn an output on has `"confirm"` |
| `all_off()` (if it has outputs) | it sends the off command for *every* output and returns True only when that worked |
| `on_connect()` / `on_release()` | whatever the first changes, the second puts back |
| `has_output` | `true` if it can source power or signal, however small |
| `tested_on` | what was actually tested, when, and how; not what it "should" work on |
| `quirks` | the surprises you found, written for the next person |

---

## Where Claude tends to go wrong

These were all seen while the catalog plugins were being written:

| What happens | Guard |
|---|---|
| Uses a sister model's manual because the file name looked right | Rule 2: check the title page |
| Cites a page from a different document | Ask it to verify each citation against *this* manual |
| Writes a blocking `READ?`, which is fine with a signal and a wedged bus without one | Step 3: the non-blocking design comes first |
| Asserts on the panel's state straight after an action, and gets the *old* value (the panel updates on its next poll, about 0.5 s later) | Wait for the next poll, or read the instrument directly |
| "All checks pass" from a check that tested nothing | Ask what the check actually exercised, and ask to see it *fail* on a deliberately broken copy. That is what `tests/test_conformance.py` does, and its first run found a real gap in the checker |
| Reports success from a test that stopped early | Ask for the full output, not the summary |
| Trusts a web search summary of a spec | Download the actual document and read it |

---

## Sharing your plugin

- It must pass `python3 tools/check_plugin.py catalog/[id]` with no MUST failures.
- `tested_on` must be honest. An untested connection type goes in `connections`
  only if you say so in the README.
- **No addresses** in the plugin. The checker refuses IP and GPIB addresses; they
  belong in each user's `site.json`.
- Zip the folder (the files at the top level or inside one folder) and share the zip.
  The recipient installs it with **Instruments → Check this .zip**, sees exactly what's
  in it, and confirms.
