// What the words heard over a paused reply say about it (#632). Pure, per
// the house rule: replyCut.js and voice.js act only on what this returns.
//
// A sound that might be someone talking pauses the reply, and words in it
// can cut the reply off. Not every word should. A phone service that cut
// at the first word found only 11% of its cuts were real interruptions:
// the rest were "yeah"s, remarks to someone else and noise (#631). So the
// words are judged:
//
//   cut     a stop word or a seat's name first ("wait", "Claude, ..."),
//           an answer to what the AI just asked, or two words of their own
//   aside   a person's name first ("Sam, pass the salt"): it's for them
//   listen  only "yeah", "mm-hm", "right" and the like: they're listening
//   more    one word of their own, or only "um" and "uh", so far
//   none    no words at all
//
// `content` counts the words that are neither listening noises nor
// fillers, so the caller can tell "um" from "tomorrow".

// Said on its own, each of these means "go on, I'm listening".
const LISTEN_WORDS = new Set([
  'yeah', 'yep', 'yup', 'ya', 'yah', 'yeh', 'mm', 'mmm', 'mhm', 'mmhm', 'mmhmm',
  'hmm', 'hm', 'uhhuh', 'right', 'okay', 'ok', 'k', 'sure', 'wow', 'oh', 'ah',
  'aha', 'ooh', 'oo', 'cool', 'nice', 'great', 'true', 'exactly', 'totally',
  'gotcha', 'interesting', 'ha', 'haha', 'hah', 'alright', 'cheers', 'thanks',
  'perfect', 'awesome', 'lovely', 'brilliant', 'sweet', 'indeed', 'absolutely',
  'definitely', 'really', 'yes',
])

// The same, as phrases. Matched before single words, so "oh no" is a
// reaction while "no" alone is still an answer.
const LISTEN_PHRASES = [
  ['mm', 'hm'], ['mm', 'hmm'], ['uh', 'huh'], ['i', 'see'], ['got', 'it'],
  ['makes', 'sense'], ['that', 'makes', 'sense'], ['fair', 'enough'],
  ['of', 'course'], ['all', 'right'], ['thank', 'you'], ['sounds', 'good'],
  ['oh', 'no'], ['oh', 'dear'], ['oh', 'god'], ['oh', 'my', 'god'], ['oh', 'my'],
  ['no', 'way'], ["you're", 'right'], ["that's", 'right'], ["that's", 'true'],
  ['good', 'point'], ['love', 'it'], ['i', 'know'], ["that's", 'cool'],
  ['very', 'cool'], ['fair', 'point'],
]

// Hesitations: the start of a turn, not a reply to anything yet.
const FILLERS = new Set(['um', 'umm', 'uh', 'uhh', 'er', 'erm'])

// Words that can come before a stop word or a name at the start without
// changing who's being spoken to: "oh wait", "hey Claude", "no, Sam".
const LEAD_INS = new Set([
  'oh', 'um', 'umm', 'uh', 'er', 'erm', 'ah', 'hey', 'ok', 'okay', 'so', 'well',
  'but', 'and', 'no', 'yeah', 'sorry', 'oi',
])

// At the start, each of these stops the reply at once.
const STOP_PHRASES = [
  ['stop'], ['wait'], ['hang', 'on'], ['hold', 'on'], ['hold', 'up'], ['hold', 'it'],
  ['pause'], ['shush'], ['shh'], ['sh'], ['quiet'], ['be', 'quiet'], ['shut', 'up'],
  ['enough'], ['cancel'], ['never', 'mind'], ['nevermind'], ['one', 'sec'],
  ['one', 'second'], ['just', 'a', 'sec'], ['just', 'a', 'second'],
  ['excuse', 'me'], ['no', 'no'],
]

// Words that answer a question: after the AI asks something, any of these
// is a reply to it, not a listening noise.
const ANSWER_WORDS = new Set([
  'yes', 'no', 'yeah', 'yep', 'yup', 'nope', 'nah', 'sure', 'okay', 'ok',
  'alright', 'please', 'correct', 'definitely', 'absolutely', 'mhm', 'mmhm',
  'mmhmm', 'uhhuh',
])
const ANSWER_PHRASES = [
  ['mm', 'hm'], ['mm', 'hmm'], ['uh', 'huh'], ['of', 'course'], ['all', 'right'],
  ['sounds', 'good'],
]

// Names that are also everyday words. One of these at the start counts as
// a name only with a comma after it, so "will you" stays a question.
const WORD_NAMES = new Set([
  'will', 'may', 'mark', 'pat', 'bill', 'rose', 'grace', 'hope', 'faith', 'april',
  'june', 'dawn', 'ray', 'jack', 'frank', 'sue', 'art', 'eve', 'rob', 'guy', 'sky',
  'summer', 'holly', 'ivy', 'max', 'norm', 'chase', 'cliff', 'dean', 'drew', 'gene',
  'glen', 'rich', 'sandy', 'sunny', 'wade', 'joy', 'amber', 'pearl', 'ruby', 'daisy',
  'lily', 'violet', 'autumn', 'hunter', 'mason', 'page', 'chance', 'chip', 'carol',
  'penny', 'robin', 'heather', 'iris', 'jade', 'olive', 'river', 'storm', 'win',
])

// A transcript as words: lower case, bracketed tags like "(sizzling)"
// dropped, a run of single letters joined ("G P T" is "gpt"), and each
// word marked when a comma follows it.
export function words(text) {
  const plain = String(text || '').replace(/\([^)]*\)|\[[^\]]*\]/g, ' ').toLowerCase()
  const out = []
  const re = /[\p{L}\p{N}]+(?:['’-][\p{L}\p{N}]+)*/gu
  let m
  while ((m = re.exec(plain))) {
    const w = m[0].replace(/’/g, "'").replace(/-/g, '')
    const comma = plain[re.lastIndex] === ','
    const prev = out[out.length - 1]
    if (w.length === 1 && /\p{L}/u.test(w) && prev?.letters) {
      prev.w += w
      prev.comma = comma
    } else {
      out.push({ w, comma, letters: w.length === 1 && /\p{L}/u.test(w) })
    }
  }
  return out.map(({ w, comma }) => ({ w, comma }))
}

// The words a list of names is heard as: every word of each, lower case,
// with spelt-out letters joined, as `words` reads a transcript.
export function nameWords(names) {
  const out = new Set()
  for (const name of names || []) {
    for (const { w } of words(name)) if (w.length > 1) out.add(w)
  }
  return out
}

// The seats' names as heard: each seat's display name and slug.
export function seatWords(participants) {
  return nameWords((participants || []).flatMap((p) => [p?.name, p?.slug]))
}

// The people the app knows, as heard: each one's names, the ones they were
// introduced under and the ones merged into them.
export function peopleWords(people) {
  return nameWords((people || []).flatMap((p) => [
    p?.preferred_name, p?.name, ...(p?.merged_names || []),
  ]))
}

// Whether the text the AI has said so far ends on a question.
export function endsOnQuestion(text) {
  return /\?["'”’)\]]*\s*$/.test(String(text || ''))
}

function phraseAt(ws, i, phrases) {
  for (const p of phrases) {
    if (p.every((w, k) => ws[i + k]?.w === w)) return p.length
  }
  return 0
}

// `seats` and `people` are sets of name words (seatWords, peopleWords).
// `asked`: the AI's last words were a question.
export function judgeWords(text, { seats = new Set(), people = new Set(), asked = false } = {}) {
  const ws = words(text)
  if (!ws.length) return { verdict: 'none', content: 0 }
  // "No, no" and the like, before lead-ins are skipped.
  if (phraseAt(ws, 0, STOP_PHRASES)) return { verdict: 'cut', content: ws.length }
  let i = 0
  while (i < ws.length - 1 && LEAD_INS.has(ws[i].w)) i++
  if (phraseAt(ws, i, STOP_PHRASES)) return { verdict: 'cut', content: ws.length }
  const first = ws[i]
  if (seats.has(first.w)) return { verdict: 'cut', content: ws.length }
  if (people.has(first.w) && !seats.has(first.w)
      && (first.comma || i === ws.length - 1 || !WORD_NAMES.has(first.w))) {
    return { verdict: 'aside', content: ws.length }
  }
  // What's left once listening noises and hesitations are set aside.
  let content = 0
  let fillers = 0
  let answers = 0
  for (let k = 0; k < ws.length;) {
    const n = phraseAt(ws, k, LISTEN_PHRASES)
    if (n) {
      if (phraseAt(ws, k, ANSWER_PHRASES) === n) answers++
      k += n
      continue
    }
    const w = ws[k].w
    if (ANSWER_WORDS.has(w)) answers++
    if (FILLERS.has(w)) fillers++
    else if (!LISTEN_WORDS.has(w)) content++
    k++
  }
  if (asked && (answers || content)) return { verdict: 'cut', content }
  if (content >= 2) return { verdict: 'cut', content }
  if (content === 1) return { verdict: 'more', content }
  if (fillers && fillers === ws.length) return { verdict: 'more', content }
  return { verdict: 'listen', content }
}
