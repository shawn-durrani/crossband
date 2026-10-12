// Today's word rules (frontend/src/interruptWords.js) over the trial's
// moments, for eval_bargein's "rules" judge (#640). Reads the moments as
// JSON on stdin and writes each one's verdict as JSON on stdout.
import { endsOnQuestion, judgeWords, peopleWords, seatWords } from '../frontend/src/interruptWords.js'

let input = ''
for await (const chunk of process.stdin) input += chunk
const { seats, people, cases } = JSON.parse(input)
const seatSet = seatWords(seats.map((name) => ({ name, slug: name.toLowerCase() })))
const peopleSet = peopleWords(people.map((name) => ({ name, preferred_name: name })))
const out = cases.map((c) => {
  const t0 = performance.now()
  const { verdict, why } = judgeWords(c.words, { seats: seatSet, people: peopleSet,
                                                 asked: endsOnQuestion(c.ai_said) })
  return { id: c.id, verdict, why, ms: performance.now() - t0 }
})
process.stdout.write(JSON.stringify(out))
