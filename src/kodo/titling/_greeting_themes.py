"""Closing-sentence themes for :func:`kodo.titling._server.generate_greeting`.

Each entry is a clause meant to complete the sentence "For example, you can
speak of ..." in the greeter's system prompt — one is picked at random per
call (:func:`random.choice`) so consecutive brand-new sessions don't all get
the same flavor of opening line. Deliberately broad and a little whimsical
(mood, industry, historical invention, an unsolved problem in computer
science, a quantum-mechanics paradox, ...) — the greeting is a one-off
pleasantry, not a claim the model actually knows deep physics or is about to
start lecturing on it.
"""

from __future__ import annotations

GREETING_THEMES: tuple[str, ...] = (
    # Moods / states of mind
    "the quiet optimism of turning to a blank page",
    "the restless energy of a workshop right before the doors open",
    "the calm of a well-organized desk at the start of the day",
    "the satisfaction of finally untangling a stubborn knot",
    "the anticipation of the first line of a new story",
    "the steady confidence of a craftsperson picking up a familiar tool",
    "the curiosity of opening a door you've never opened before",
    "the small thrill of a plan finally coming together",
    # Industries / trades
    "the precise choreography of an orchestra tuning before a performance",
    "the patient rhythm of a bakery in the hours before dawn",
    "the quiet discipline of a lighthouse keeper's nightly rounds",
    "the improvisational trust between musicians in a jazz quartet",
    "the meticulous bookkeeping of an old-world clockmaker",
    "the choreography of an air-traffic control tower on a busy morning",
    "the careful blend of a tea master judging leaves by scent alone",
    "the split-second teamwork of a Formula 1 pit crew",
    "the deliberate pacing of a chess grandmaster reading the board",
    # Major inventions and their ripple effects
    "the printing press and how it rewired the flow of human knowledge",
    "the invention of the wheel and the deceptively simple idea behind it",
    "the discovery of penicillin and the accident that started it",
    "the transistor and how something so small reshaped the world",
    "the telegraph and the first time distance stopped mattering",
    "the steam engine and the century it set into motion",
    "the compass and how it let sailors trust the unseen",
    "the invention of zero and the strange power of naming nothing",
    "the light bulb and the thousand quiet failures before it worked",
    # Unsolved problems in computer science and mathematics
    "the P versus NP problem, and whether every quickly checked answer has a quickly found one",
    "the halting problem, and the proof that some questions can never be answered in general",
    "the traveling salesman problem, deceptively simple and stubbornly hard",
    "the Collatz conjecture, an arithmetic riddle no one has managed to crack",
    "the Riemann hypothesis, quietly guarding the secrets of prime numbers",
    "the question of whether P equals NP, still open after half a century",
    "the twin prime conjecture, and the primes that keep almost proving it",
    "the graph isomorphism problem, sitting in its own curious limbo",
    "the busy beaver problem, where the numbers grow faster than imagination",
    # Paradoxes and puzzles of quantum mechanics
    "Schrödinger's cat, suspended between two fates until someone looks",
    "quantum entanglement's spooky action at a distance",
    "the double-slit experiment and light's refusal to pick a lane",
    "the observer effect, where looking changes what you find",
    "the EPR paradox and Einstein's stubborn discomfort with uncertainty",
    "Heisenberg's uncertainty principle, where knowing one thing means not knowing another",
    "quantum tunneling, where particles slip through walls they shouldn't cross",
    "the many-worlds interpretation, where every choice quietly forks the universe",
    # Nature and natural phenomena
    "the patient architecture of a spider's web at dawn",
    "the improbable navigation of a monarch butterfly's migration",
    "the slow geometry of a nautilus shell's spiral",
    "the hidden network of fungi connecting a forest's roots",
    "the quiet resilience of a coral reef rebuilding itself",
    "the precise timing of cicadas emerging after seventeen years underground",
    "the strange physics of a soap bubble finding the least possible surface",
    "the long memory locked inside a single tree's rings",
    # Exploration, history, and craft
    "the mapmakers who once had to guess at the edges of the world",
    "the first calculation that put a satellite into orbit",
    "the ancient library at Alexandria and everything it tried to hold",
    "the stonemasons who built cathedrals they'd never live to see finished",
    "the code breakers who spent their war years arguing with silence",
    'the cartographers who left "here be dragons" where their knowledge ran out',
    "the quiet stubbornness of the first people to cross an unmapped ocean",
    "the archivists who spend careers preserving stories that aren't their own",
    # A few playful, lighter ones
    "the improbable comfort of a perfectly organized junk drawer",
    "the small triumph of a recipe that finally comes out right",
    "the odd satisfaction of a puzzle piece clicking into place",
    "the surprising complexity hiding inside something as simple as a paperclip",
    "the calm before a gardener's first seed of spring goes into the soil",
)
