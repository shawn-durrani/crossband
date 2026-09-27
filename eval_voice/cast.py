"""The made-up people and the stock ElevenLabs voice each one speaks in.

The names are the synthetic roster from the pull request template, and
nobody else ever speaks in the rig. Each voice is one of ElevenLabs'
premade voices, which every account can use. Dave and Mateo are both
men with American accents, picked so a run has one pair that's hard to
tell apart.

The owner is the person the isolated app is set up for, the one whose
voice it treats as "you". The enrolment passage is what each person
reads aloud once, through the app's own recording route, before any
conversation, the way you'd record someone on the Voices page.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Voice:
    voice_id: str
    stock_name: str
    description: str


CAST = {
    "Alex": Voice("IKne3meq5aSn9XLyUdCD", "Charlie",
                  "male, Australian, young"),
    "Sam": Voice("cgSgspJ2msm6clMCkdW9", "Jessica",
                 "female, American, young"),
    "Dave": Voice("iP95p4xoKVk53GoZ742B", "Chris",
                  "male, American, middle aged"),
    "Mateo": Voice("bIHbv24MWmeRgasZH58o", "Will",
                   "male, American, young"),
}

OWNER = "Alex"

# Who has a voice bank before the first conversation. Mateo is left out,
# so a script can meet him as a new voice, or introduce him first.
DEFAULT_ENROLLED = ("Alex", "Sam", "Dave")

# About 30 seconds each when read aloud. The recording route needs 10
# seconds of speech at least and takes a minute at most.
ENROL_PASSAGES = {
    "Alex": (
        "On Saturday morning I walked down to the market before it got busy. "
        "The bread stall had run out of sourdough again, so I bought a rye "
        "loaf and a bag of apples instead. On the way home I stopped to "
        "watch two dogs chase each other around the park, and I lost track "
        "of time. By the time I got back, the coffee I had left on the "
        "bench was completely cold, and I had to start the whole thing over."
    ),
    "Sam": (
        "The library near my place has a reading room with tall windows and "
        "a clock that runs about four minutes fast. I like to sit at the "
        "long table near the back, where the light is softer in the "
        "afternoon. Last week I found an old atlas that still showed "
        "countries that no longer exist, and I spent an hour tracing rivers "
        "with my finger. Nobody asked me to leave, so I stayed until they "
        "switched the lights off."
    ),
    "Dave": (
        "I have been trying to fix the back fence for three weekends now. "
        "The first weekend it rained, the second weekend I bought the wrong "
        "screws, and last weekend the neighbour's cat sat on the timber and "
        "refused to move. I finally got two panels up on Sunday afternoon. "
        "They are not perfectly straight, but they keep the wind out, and "
        "honestly that is all I wanted from them in the first place."
    ),
    "Mateo": (
        "Every summer we drive up the coast to a campsite with no phone "
        "signal at all. The first day always feels strange, like you have "
        "forgotten something important. By the second day you stop "
        "reaching for your pocket. We cook on a tiny gas stove, swim before "
        "breakfast, and play cards by torchlight until everyone gets too "
        "sleepy to count their points. It is the quietest week of the year."
    ),
}


def roster() -> tuple:
    return tuple(CAST)
