/**
 * TheoryInvestigator — Runnable wrapper around the theory-driven decoding loop.
 *
 * Exposes conversational tools for targeted decipherment work:
 *   - propose_theory: Generate a new decoding theory for a specific folio
 *   - challenge_theory: Run the skeptic against a theory
 *   - run_theory_loop: Batch-run the full theory loop for N rounds
 *   - list_theories: Show the best theories found so far
 *
 * Implements the Runnable interface so it can be composed in RouterAgent
 * or any other workflow agent.
 */

import { z } from 'zod';
import type { AgentTool } from '../../../src/agent/tools.js';
import { defineTool } from '../../../src/agent/tools.js';
import type { Message, Runnable } from '../../../src/workflows/types.js';
import {
  loadFolios,
  proposeTheory,
  challengeTheory,
  runTheoryLoop,
} from './theory-loop.js';
import type { Theory } from './theory-loop.js';

export class TheoryInvestigator implements Runnable {
  private theories: Theory[] = [];
  private _tools: AgentTool[];

  constructor() {
    this._tools = this.buildTools();
  }

  async run(messages: Message[]): Promise<string> {
    const last = messages[messages.length - 1]?.content ?? '';

    // If the message looks like a direct command, handle it
    if (last.toLowerCase().includes('propose') || last.toLowerCase().includes('try')) {
      return 'Ready to investigate Voynich theories. Use the propose_theory tool to generate a new decoding theory, or run_theory_loop to batch-test multiple hypotheses.';
    }

    return [
      'Theory Investigator ready.',
      `${this.theories.length} theories explored so far.`,
      'Available actions: propose_theory, challenge_theory, run_theory_loop, list_theories.',
    ].join(' ');
  }

  async *stream(messages: Message[]): AsyncGenerator<string> {
    yield await this.run(messages);
  }

  collectTools(): AgentTool[] {
    return this._tools;
  }

  private buildTools(): AgentTool[] {
    return [
      defineTool({
        name: 'propose_theory',
        description:
          'Propose a new decoding theory for a specific Voynich folio. ' +
          'Generates a symbol map, decodes the text, tests cross-folio consistency, ' +
          'and runs the skeptic challenge.',
        parameters: z.object({
          folio_id: z.string().optional().describe('Target folio ID (e.g. "f1r"). If omitted, picks a random high-confidence folio.'),
          source_language: z.enum(['latin', 'italian', 'greek', 'hebrew', 'arabic', 'occitan', 'catalan', 'czech'])
            .default('latin')
            .describe('Candidate source language for the plaintext'),
          cipher_type: z.enum(['substitution', 'polyalphabetic'])
            .default('substitution')
            .describe('Cipher type hypothesis'),
        }),
        handler: async ({ folio_id, source_language, cipher_type }) => {
          const folios = await loadFolios();
          const highConfidence = folios.filter((f) => f.confidence >= 0.5);

          const target = folio_id
            ? folios.find((f) => f.folio_id === folio_id)
            : highConfidence[Math.floor(Math.random() * highConfidence.length)];

          if (!target) {
            return { error: `Folio "${folio_id}" not found. Available: ${folios.slice(0, 10).map((f) => f.folio_id).join(', ')}...` };
          }

          const theory = await proposeTheory(target, folios, source_language, cipher_type);
          this.theories.push(theory);

          // Auto-challenge
          let verdict = 'unknown';
          let objection = '';
          try {
            const challenge = await challengeTheory(theory, folios);
            const cleaned = challenge.replace(/```json?\s*/g, '').replace(/```/g, '').trim();
            const parsed = JSON.parse(cleaned);
            verdict = parsed.verdict ?? 'unknown';
            objection = parsed.strongest_objection ?? '';
          } catch {
            // Skeptic parse failed — continue with unknown verdict
          }

          return {
            theory_id: theory.id,
            folio: theory.target_folio,
            plant: theory.target_plant,
            language: theory.source_language,
            cipher_type: theory.cipher_type,
            decoded_text: theory.decoded_text.slice(0, 200),
            grounding_score: theory.grounding_score,
            consistency_score: theory.consistency_score,
            cross_folio_tested: theory.cross_folio_results.length,
            skeptic_verdict: verdict,
            skeptic_objection: objection.slice(0, 200),
            symbol_map_size: Object.keys(theory.symbol_map).length,
          };
        },
      }),

      defineTool({
        name: 'challenge_theory',
        description: 'Run the skeptic against a specific theory by ID to check for weaknesses.',
        parameters: z.object({
          theory_id: z.string().describe('ID of the theory to challenge'),
        }),
        handler: async ({ theory_id }) => {
          const theory = this.theories.find((t) => t.id === theory_id);
          if (!theory) {
            return { error: `Theory "${theory_id}" not found. Use list_theories to see available IDs.` };
          }

          const folios = await loadFolios();
          const challenge = await challengeTheory(theory, folios);

          try {
            const cleaned = challenge.replace(/```json?\s*/g, '').replace(/```/g, '').trim();
            return JSON.parse(cleaned);
          } catch {
            return { raw_response: challenge.slice(0, 500) };
          }
        },
      }),

      defineTool({
        name: 'run_theory_loop',
        description:
          'Batch-run the theory loop for N rounds, testing random folios with random languages. ' +
          'Returns the top theories sorted by combined grounding + consistency score.',
        parameters: z.object({
          rounds: z.number().int().min(1).max(50).default(10).describe('Number of theory rounds to run'),
        }),
        handler: async ({ rounds }) => {
          const results = await runTheoryLoop(rounds);
          this.theories.push(...results);

          const top5 = results.slice(0, 5);
          return {
            total_tested: results.length,
            top_theories: top5.map((t) => ({
              id: t.id,
              folio: t.target_folio,
              plant: t.target_plant,
              language: t.source_language,
              cipher_type: t.cipher_type,
              grounding: t.grounding_score,
              consistency: t.consistency_score,
              combined: t.grounding_score + t.consistency_score,
              decoded_preview: t.decoded_text.slice(0, 80),
            })),
          };
        },
      }),

      defineTool({
        name: 'list_theories',
        description: 'List the best theories found so far, sorted by combined score.',
        parameters: z.object({
          top_n: z.number().int().min(1).max(50).default(10).describe('Number of theories to return'),
        }),
        handler: async ({ top_n }) => {
          if (this.theories.length === 0) {
            return { message: 'No theories explored yet. Use propose_theory or run_theory_loop first.' };
          }

          const sorted = [...this.theories].sort(
            (a, b) => (b.grounding_score + b.consistency_score) - (a.grounding_score + a.consistency_score),
          );

          return {
            total: this.theories.length,
            showing: Math.min(top_n, sorted.length),
            theories: sorted.slice(0, top_n).map((t) => ({
              id: t.id,
              folio: t.target_folio,
              plant: t.target_plant,
              language: t.source_language,
              cipher_type: t.cipher_type,
              grounding: t.grounding_score,
              consistency: t.consistency_score,
              combined: t.grounding_score + t.consistency_score,
              decoded_preview: t.decoded_text.slice(0, 80),
            })),
          };
        },
      }),
    ];
  }
}
