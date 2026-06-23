"""Unit tests for the pure helper functions in the pipeline.

These cover the markdown/frontmatter rendering, tagging, slug/filename safety,
bundle splitting, and the synthesize model-JSON parser. Everything tested here
is synchronous and side-effect free (except unique_path, which only touches a
temp directory). Nothing here touches Telegram, the network, or a subprocess.

Run with: python -m unittest discover -s tests -v
"""

import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make the repo root importable when tests are run via `discover -s tests`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_context as tc  # noqa: E402
import synthesize as syn  # noqa: E402
from telegram.constants import MessageOriginType  # noqa: E402


# --- Lightweight fakes for Telegram Message objects -------------------------
#
# The real handlers receive telegram.Message instances, but the pure helpers
# only read a handful of attributes. These fakes expose exactly those and
# default every other attribute to None, so media_marker / message_image treat
# the message as a plain text message rather than raising AttributeError.

_TZ = timezone.utc


class _FakeUser:
    def __init__(self, full_name):
        self.full_name = full_name


class _FakeForwardUserOrigin:
    """A forward_origin of type USER (the common 'forwarded from a person' case)."""

    type = MessageOriginType.USER

    def __init__(self, full_name, date):
        self.sender_user = _FakeUser(full_name)
        self.date = date


class _FakeMessage:
    """Minimal stand-in for telegram.Message.

    Any attribute not set in __init__ resolves to None via __getattr__, which is
    what message_image / media_marker expect for a text-only message.
    """

    def __init__(self, text=None, caption=None, from_name="Me",
                 forward_origin=None, date=None):
        self.text = text
        self.caption = caption
        self.from_user = _FakeUser(from_name) if from_name is not None else None
        self.forward_origin = forward_origin
        self.date = date if date is not None else datetime(2024, 1, 1, 12, 0, tzinfo=_TZ)

    def __getattr__(self, name):
        # photo, document, voice, video, sticker, poll, etc. -> None
        return None


def _forwarded(text, who="Alice", date=None):
    """Build a fake forwarded message from `who`."""
    d = date if date is not None else datetime(2024, 1, 1, 11, 0, tzinfo=_TZ)
    return _FakeMessage(text=text, from_name=None,
                        forward_origin=_FakeForwardUserOrigin(who, d), date=d)


def _own(text, date=None):
    """Build a fake non-forwarded (user's own note) message."""
    return _FakeMessage(text=text, from_name="Me", forward_origin=None, date=date)


# --- extract_tags / strip_tags ---------------------------------------------

class TestTags(unittest.TestCase):
    def test_extract_tags_lowercased(self):
        self.assertEqual(tc.extract_tags("note #Foo and #BAR baz"), ["foo", "bar"])

    def test_extract_tags_none_and_empty(self):
        self.assertEqual(tc.extract_tags(""), [])
        self.assertEqual(tc.extract_tags(None), [])
        self.assertEqual(tc.extract_tags("no tags here"), [])

    def test_extract_tags_inline_and_repeated(self):
        # inline (mid-word boundary) and duplicate tags are both captured
        self.assertEqual(
            tc.extract_tags("ship#urgent then #urgent again"),
            ["urgent", "urgent"],
        )

    def test_strip_tags_removes_tags_and_collapses_space(self):
        self.assertEqual(tc.strip_tags("fix the #login #bug now"), "fix the now")

    def test_tags_stripped_from_rendered_body(self):
        # The note text feeds Context body via strip_tags; tags must not survive.
        raw = "please simplify the onboarding #ux #onboarding"
        tags = tc.extract_tags(raw)
        body = tc.strip_tags(raw)
        self.assertEqual(tags, ["ux", "onboarding"])
        self.assertNotIn("#", body)
        self.assertEqual(body, "please simplify the onboarding")


# --- slugify ----------------------------------------------------------------

class TestSlugify(unittest.TestCase):
    def test_spaces_to_hyphens_and_lowercase(self):
        self.assertEqual(tc.slugify("Hello World"), "hello-world")

    def test_strips_unsafe_chars(self):
        # punctuation / path separators / colons must not survive
        out = tc.slugify("a/b: c*?\\d <e>")
        self.assertEqual(out, "a-b-c-d-e")
        for bad in "/:*?\\<> ":
            self.assertNotIn(bad, out)

    def test_max_words_truncation(self):
        self.assertEqual(
            tc.slugify("one two three four five six seven eight"),
            "one-two-three-four-five-six",
        )
        self.assertEqual(tc.slugify("one two three four five", max_words=2), "one-two")

    def test_empty_falls_back_to_untagged(self):
        self.assertEqual(tc.slugify(""), "untagged")
        self.assertEqual(tc.slugify(None), "untagged")
        self.assertEqual(tc.slugify("!!! ??? ..."), "untagged")

    def test_unicode_dropped(self):
        # Only [a-z0-9] tokens are kept; accented/non-ascii chars are dropped.
        self.assertEqual(tc.slugify("café münchen 2024"), "caf-m-nchen-2024")


# --- parse_bundle -----------------------------------------------------------

class TestParseBundle(unittest.TestCase):
    def test_last_non_forwarded_is_the_note(self):
        fwd1 = _forwarded("first forwarded", who="Alice")
        fwd2 = _forwarded("second forwarded", who="Bob")
        note = _own("my synthesis #tag")
        messages = [fwd1, fwd2, note]

        context_msg, convo, tags, context_body = tc.parse_bundle(messages)

        self.assertIs(context_msg, note)
        self.assertEqual(convo, [fwd1, fwd2])
        self.assertEqual(tags, ["tag"])
        self.assertEqual(context_body, "my synthesis")

    def test_picks_last_when_multiple_non_forwarded(self):
        note1 = _own("earlier note")
        fwd = _forwarded("forwarded body")
        note2 = _own("later note #x")
        messages = [note1, fwd, note2]

        context_msg, convo, tags, _ = tc.parse_bundle(messages)
        self.assertIs(context_msg, note2)
        # Everything that is not the chosen context is convo, incl. the earlier note.
        self.assertIn(note1, convo)
        self.assertIn(fwd, convo)
        self.assertEqual(tags, ["x"])

    def test_no_non_forwarded_means_no_context(self):
        fwd1 = _forwarded("a")
        fwd2 = _forwarded("b")
        context_msg, convo, tags, context_body = tc.parse_bundle([fwd1, fwd2])
        self.assertIsNone(context_msg)
        self.assertEqual(convo, [fwd1, fwd2])
        self.assertEqual(tags, [])
        self.assertEqual(context_body, "")

    def test_caption_used_when_no_text(self):
        note = _FakeMessage(text=None, caption="caption note #cap", from_name="Me")
        context_msg, _, tags, body = tc.parse_bundle([note])
        self.assertIs(context_msg, note)
        self.assertEqual(tags, ["cap"])
        self.assertEqual(body, "caption note")


# --- unique_path ------------------------------------------------------------

class TestUniquePath(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_returns_base_when_absent(self):
        target = self.dir / "note.md"
        self.assertEqual(tc.unique_path(target), target)

    def test_appends_suffix_on_collision(self):
        target = self.dir / "note.md"
        target.write_text("x")
        got = tc.unique_path(target)
        self.assertEqual(got.name, "note_2.md")
        self.assertFalse(got.exists())

    def test_increments_until_free(self):
        (self.dir / "note.md").write_text("x")
        (self.dir / "note_2.md").write_text("x")
        (self.dir / "note_3.md").write_text("x")
        got = tc.unique_path(self.dir / "note.md")
        self.assertEqual(got.name, "note_4.md")


# --- render_bundle ----------------------------------------------------------

class TestRenderBundle(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2024, 3, 4, 9, 30, 0, tzinfo=_TZ)

    def _frontmatter(self, content):
        """Return the frontmatter block (lines between the first two '---')."""
        self.assertTrue(content.startswith("---\n"))
        rest = content[len("---\n"):]
        end = rest.index("\n---")
        return rest[:end].splitlines()

    def test_frontmatter_has_expected_keys(self):
        fwd = _forwarded("forwarded source", who="Alice",
                         date=datetime(2024, 3, 4, 9, 0, tzinfo=_TZ))
        note = _own("the note #alpha #beta",
                    date=datetime(2024, 3, 4, 9, 5, tzinfo=_TZ))
        content = tc.render_bundle([fwd, note], self.now, image_map={})

        fm = self._frontmatter(content)
        keys = {line.split(":", 1)[0] for line in fm}
        self.assertEqual(
            keys,
            {"date", "source", "tags", "participants",
             "forward_count", "screenshot_count", "window_seconds"},
        )
        fm_text = "\n".join(fm)
        self.assertIn("source: telegram", fm_text)
        self.assertIn("tags: [alpha, beta]", fm_text)
        self.assertIn("forward_count: 1", fm_text)
        self.assertIn("screenshot_count: 0", fm_text)
        # date is the `now` passed in, ISO formatted to seconds.
        self.assertIn(f"date: {self.now.isoformat(timespec='seconds')}", fm_text)

    def test_includes_context_and_conversation_sections(self):
        fwd = _forwarded("forwarded source material", who="Alice")
        note = _own("please look at this #review")
        content = tc.render_bundle([fwd, note], self.now, image_map={})

        self.assertIn("# Context", content)
        self.assertIn("please look at this", content)  # body, tags stripped
        self.assertNotIn("#review", content)           # tag not in body
        self.assertIn("# Conversation", content)
        self.assertIn("forwarded source material", content)
        self.assertIn("**Alice**", content)            # forwarded author rendered

    def test_empty_bundle_marker(self):
        # No context body, no convo -> the explicit empty-bundle marker.
        bare = _own(None)
        content = tc.render_bundle([bare], self.now, image_map={})
        self.assertIn("# (empty bundle)", content)

    def test_window_seconds_from_convo_span(self):
        fwd1 = _forwarded("a", date=datetime(2024, 3, 4, 9, 0, 0, tzinfo=_TZ))
        fwd2 = _forwarded("b", date=datetime(2024, 3, 4, 9, 0, 30, tzinfo=_TZ))
        note = _own("note")
        content = tc.render_bundle([fwd1, fwd2, note], self.now, image_map={})
        self.assertIn("window_seconds: 30", content)


# --- synthesize._parse_model_json -------------------------------------------

class TestParseModelJson(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(syn._parse_model_json('{"a": 1, "b": "x"}'),
                         {"a": 1, "b": "x"})

    def test_strips_code_fences(self):
        text = '```json\n{"classification": "new", "verdict": "ok"}\n```'
        self.assertEqual(
            syn._parse_model_json(text),
            {"classification": "new", "verdict": "ok"},
        )

    def test_extracts_object_from_surrounding_prose(self):
        text = 'Here is the result:\n{"verdict": "done"}\nThanks!'
        self.assertEqual(syn._parse_model_json(text), {"verdict": "done"})

    def test_malformed_returns_none_not_raises(self):
        self.assertIsNone(syn._parse_model_json("not json at all"))
        self.assertIsNone(syn._parse_model_json(""))
        self.assertIsNone(syn._parse_model_json("{ broken: }"))
        self.assertIsNone(syn._parse_model_json("```\nstill not json\n```"))

    def test_real_envelope_shape(self):
        # The shape _run_claude expects from the model after envelope unwrap.
        text = (
            '{"classification": "duplicate", '
            '"verdict": "♻️ same onboarding ask as before", '
            '"synthesis_md": "# Product feedback synthesis\\n..."}'
        )
        data = syn._parse_model_json(text)
        self.assertIsNotNone(data)
        self.assertEqual(data["classification"], "duplicate")
        self.assertIn("synthesis_md", data)
        self.assertIn("verdict", data)


if __name__ == "__main__":
    unittest.main()
