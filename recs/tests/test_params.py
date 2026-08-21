"""The tuning surface.

Two things are asserted, and the second is the unusual one.

**Overrides work, and typos do not silently pass.** A weight that can be set
but has no effect is the worst possible tuning knob, because it looks like the
value was tried and rejected.

**The weights are not scattered through the code.** `params.py` is supposed to
be the only place a scoring number lives, and that claim decays the moment
somebody adds `* 1.4` to a scorer during a debugging session. This scans for
it — imperfectly, since arithmetic is unavoidable in an estimator, but well
enough to catch a *weight* being introduced outside the parameters.
"""
import io
import pathlib
import re
import tokenize

from django.test import TestCase, override_settings

from ..params import RecsParams, ScoringWeights, load_params
from ..scoring import HEADS

PARAMS_DIR = pathlib.Path(__file__).resolve().parent.parent


class LoadingTests(TestCase):
    def test_defaults_load(self):
        params = load_params()
        self.assertIsInstance(params, RecsParams)
        self.assertIsInstance(params.weights, ScoringWeights)

    @override_settings(SWIFT_RECS={'weights': {'like': 99.0}})
    def test_a_single_weight_can_be_overridden(self):
        params = load_params()
        self.assertEqual(params.weights.like, 99.0)
        # And the rest keep their defaults.
        self.assertEqual(params.weights.comment, ScoringWeights().comment)

    @override_settings(SWIFT_RECS={'diversity': {'exploration_rate': 0.4}})
    def test_sections_are_independent(self):
        params = load_params()
        self.assertEqual(params.diversity.exploration_rate, 0.4)
        self.assertEqual(params.weights.like, ScoringWeights().like)

    @override_settings(SWIFT_RECS={'weights': {'lke': 1.0}})
    def test_a_typo_raises_rather_than_being_ignored(self):
        with self.assertRaises(ValueError) as caught:
            load_params()
        self.assertIn('lke', str(caught.exception))

    @override_settings(SWIFT_RECS={'wieghts': {'like': 1.0}})
    def test_an_unknown_section_raises(self):
        with self.assertRaises(ValueError):
            load_params()

    def test_params_are_immutable(self):
        params = load_params()
        with self.assertRaises(Exception):
            params.weights.like = 1.0

    def test_every_scoring_head_has_a_weight(self):
        """A head with no weight scores as an AttributeError, not as zero —
        but only when that head fires, which could be much later."""
        weights = ScoringWeights()
        for head in HEADS:
            self.assertTrue(
                hasattr(weights, head), f'no weight declared for head {head!r}',
            )

    def test_every_weight_corresponds_to_a_head(self):
        """The other direction: a weight nothing reads is a knob that does
        nothing, which is the failure this whole module exists to prevent."""
        from dataclasses import fields

        for field in fields(ScoringWeights):
            self.assertIn(
                field.name, HEADS, f'weight {field.name!r} is not a scoring head',
            )

    def test_negative_heads_carry_negative_weights(self):
        weights = ScoringWeights()
        for head in ('skip', 'negative_feedback', 'creator_fatigue',
                     'topic_fatigue', 'seen_before'):
            self.assertLess(getattr(weights, head), 0, head)

    def test_watching_outweighs_any_single_tap(self):
        """A feed optimised for likes becomes a feed of bait.

        Stated as a test because it is a product decision that a plausible
        tuning session could reverse by accident.
        """
        weights = ScoringWeights()
        for tap in ('like', 'comment', 'repost', 'profile_open', 'location_reveal'):
            self.assertGreater(weights.completion, getattr(weights, tap), tap)


class MagicNumberTests(TestCase):
    """No scoring weight outside `params.py`.

    The scan is heuristic and deliberately narrow: it looks for a float literal
    being *assigned to a module-level name* in the ranking modules, which is
    what a stray weight looks like. Arithmetic inside the estimators is
    expected and not flagged — those are model shape, not tuning.
    """

    RANKING_MODULES = ('scoring.py', 'diversity.py', 'candidates.py', 'pipeline.py')
    ASSIGNMENT = re.compile(r'^([A-Z_][A-Z0-9_]*)\s*=\s*-?\d+\.\d+\s*$')

    #: Named, documented exceptions. Each is a property of the *estimator*
    #: rather than a tuning weight — changing them changes what the model is,
    #: not how much a signal is worth.
    ALLOWED = set()

    def test_no_module_level_float_constants_outside_params(self):
        offenders = []
        for name in self.RANKING_MODULES:
            path = PARAMS_DIR / name
            with io.open(path, encoding='utf-8') as handle:
                for line in handle:
                    match = self.ASSIGNMENT.match(line.strip())
                    if match and match.group(1) not in self.ALLOWED:
                        offenders.append(f'{name}: {line.strip()}')
        self.assertEqual(
            offenders, [],
            f'scoring constants belong in params.py: {offenders}',
        )

    def test_params_is_the_only_module_that_reads_settings(self):
        """One door for configuration.

        A second module reading `settings.SWIFT_RECS` directly would be a
        second place the defaults live, and the two would diverge.
        """
        offenders = []
        for path in PARAMS_DIR.glob('*.py'):
            if path.name == 'params.py':
                continue
            with io.open(path, encoding='utf-8') as handle:
                tokens = [
                    t.string for t in tokenize.generate_tokens(handle.readline)
                    if t.type not in (tokenize.COMMENT, tokenize.STRING)
                ]
            if 'SWIFT_RECS' in tokens:
                offenders.append(path.name)
        self.assertEqual(offenders, [])
