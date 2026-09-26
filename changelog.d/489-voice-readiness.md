- The Voices page can now say whether each stored voice is ready, which
  means the app should name that person on a day it hasn't heard them
  yet. It cuts their stored speech into 2 second pieces, leaves each
  day's clips out in turn, and checks those pieces are still named as
  them, and never as anyone else. It's off until you set
  `voice_calibrated_scorer`, and then it downloads a second speaker
  model once, about 26MB. It works in the background and changes
  nothing about how turns are named (#489).
