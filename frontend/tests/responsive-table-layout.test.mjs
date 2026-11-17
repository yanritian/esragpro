import assert from 'node:assert/strict'
import { describe, test } from 'node:test'
import { readFileSync } from 'node:fs'

const documentsSource = readFileSync(
  new URL('../src/views/Documents.vue', import.meta.url),
  'utf8',
)
const evaluationSource = readFileSync(
  new URL('../src/views/Evaluation.vue', import.meta.url),
  'utf8',
)

function getMobileCss(source, breakpoint) {
  const match = source.match(
    new RegExp(`@media\\s*\\(max-width:\\s*${breakpoint}px\\)\\s*\\{([\\s\\S]*)\\n\\}`),
  )
  assert.ok(match, `missing ${breakpoint}px responsive media block`)
  return match[1]
}

function getMobileList(source, className) {
  const start = source.indexOf(`class="${className}"`)
  assert.notEqual(start, -1, `missing ${className}`)
  const end = source.indexOf('\n        </el-card>', start)
  assert.ok(end > start, `could not delimit ${className}`)
  return source.slice(start, end)
}

describe('responsive data views', () => {
  test('keeps every document field and action in a wrapping mobile list', () => {
    const mobileCss = getMobileCss(documentsSource, 768)
    const mobileList = getMobileList(documentsSource, 'documents-mobile-list')

    for (const field of [
      'row.title',
      'row.source',
      'row.doc_type',
      'formatDate(row.created_at)',
      'handleDelete(row.doc_id)',
    ]) {
      assert.ok(mobileList.includes(field), `mobile documents omit ${field}`)
    }

    assert.match(mobileCss, /\.documents-table\s*\{\s*display:\s*none;/)
    assert.match(mobileCss, /\.documents-mobile-list\s*\{\s*display:\s*block;/)
    assert.match(documentsSource, /\.document-field-value[\s\S]*?overflow-wrap:\s*anywhere;/)
    assert.doesNotMatch(mobileCss, /overflow-x\s*:\s*auto/)
  })

  test('keeps every evaluation table field in mobile lists without horizontal scrolling', () => {
    const mobileCss = getMobileCss(evaluationSource, 768)
    const mobileLists = [
      ['test-cases-mobile', ['row.id', 'row.category']],
      [
        'category-data-mobile',
        [
          'row.category',
          'row.total',
          'row.correct',
          'row.accuracy',
          'row.hallucination',
          'row.hallucination_rate',
          'row.avg_confidence',
          'row.avg_response_time',
        ],
      ],
      [
        'report-details-mobile',
        [
          'row.test_id',
          'row.question',
          'row.status',
          'row.is_correct',
          'row.has_hallucination',
          'row.confidence',
        ],
      ],
    ]

    assert.equal(
      (evaluationSource.match(/class="evaluation-mobile-list /g) || []).length,
      3,
    )

    for (const [className, fields] of mobileLists) {
      const mobileList = getMobileList(
        evaluationSource,
        `evaluation-mobile-list ${className}`,
      )
      for (const field of fields) {
        assert.ok(mobileList.includes(field), `${className} omits ${field}`)
      }
    }

    assert.match(mobileCss, /\.evaluation-table\s*\{\s*display:\s*none;/)
    assert.match(mobileCss, /\.evaluation-mobile-list\s*\{\s*display:\s*block;/)
    assert.match(mobileCss, /overflow-wrap:\s*anywhere;/)
    assert.doesNotMatch(mobileCss, /overflow-x\s*:\s*auto/)
  })
})
