import { describe, expect, test } from 'vitest'

describe('vitest infrastructure', () => {
  test('runs TypeScript tests', () => {
    const value: number = 1 + 1
    expect(value).toBe(2)
  })
})
