/**
 * Doc Parser connector tools.
 *
 * Provides utilities and agent tools for uploading documents, chunking text,
 * and extracting entities via LLM from document content stored in UC Volumes.
 *
 *  - chunkText             — pure function: split text into overlapping chunks
 *  - createDocUploadTool   — upload a document to a UC Volume via Files API
 *  - createDocChunkTool    — chunk text using schema-configured chunk settings
 *  - createDocExtractEntitiesTool — extract entities from chunks via FMAPI
 */

import { randomUUID } from 'node:crypto';
import { z } from 'zod';
import { defineTool, type AgentTool } from '../agent/tools.js';
import { resolveHost, resolveToken, type ConnectorConfig } from '../connectors/types.js';

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Chunk {
  chunk_id: string;
  text: string;
  position: number;
}

// ---------------------------------------------------------------------------
// Pure helpers
// ---------------------------------------------------------------------------


// ---------------------------------------------------------------------------
// chunkText
// ---------------------------------------------------------------------------

/**
 * Split `text` into overlapping chunks.
 *
 * @param text         - Input text to chunk
 * @param chunkSize    - Maximum characters per chunk
 * @param chunkOverlap - Number of characters to overlap between consecutive chunks
 * @returns Array of Chunk objects with sequential chunk_ids and byte positions
 */
export function chunkText(text: string, chunkSize: number, chunkOverlap: number): Chunk[] {
  if (!text) return [];

  // Guard: if overlap >= size the window would never advance → single chunk
  const step = chunkOverlap >= chunkSize ? chunkSize : chunkSize - chunkOverlap;

  const chunks: Chunk[] = [];
  let position = 0;
  let index = 0;

  while (position < text.length) {
    const slice = text.slice(position, position + chunkSize);
    chunks.push({
      chunk_id: `chunk_${index}`,
      text: slice,
      position,
    });
    index++;
    position += step;
  }

  return chunks;
}

// ---------------------------------------------------------------------------
// createDocUploadTool
// ---------------------------------------------------------------------------

/**
 * Create a tool that uploads a document to a UC Volume via the Files API.
 * Requires `config.volumePath` to be set.
 */
export function createDocUploadTool(config: ConnectorConfig): AgentTool {
  if (!config.volumePath) {
    throw new Error('volumePath is required in ConnectorConfig for createDocUploadTool');
  }

  const volumePath = config.volumePath;

  return defineTool({
    name: 'doc_upload',
    description: 'Upload a document to a Unity Catalog Volume via the Databricks Files API.',
    parameters: z.object({
      filename: z.string().describe('Name for the file in the volume'),
      content: z.string().describe('File content to upload'),
    }),
    handler: async ({ filename, content }) => {
      const host = resolveHost(config.host);
      const token = await resolveToken();
      const docId = randomUUID();

      // Strip trailing slash from volumePath, then build path
      const base = volumePath.replace(/\/$/, '');
      const remotePath = `${base}/${docId}_${filename}`;

      const url = `${host}/api/2.0/fs/files${remotePath}`;

      const res = await fetch(url, {
        method: 'PUT',
        headers: {
          Authorization: `Bearer ${token}`,
          'Content-Type': 'application/octet-stream',
        },
        body: content,
      });

      if (!res.ok) {
        const text = await res.text();
        throw new Error(`Files API PUT ${res.status}: ${text}`);
      }

      return {
        doc_id: docId,
        path: remotePath,
        filename,
        size: content.length,
      };
    },
  });
}

// ---------------------------------------------------------------------------
// createDocChunkTool
// ---------------------------------------------------------------------------

/**
 * Create a tool that splits text into chunks using schema-configured settings.
 */
export function createDocChunkTool(config: ConnectorConfig): AgentTool {
  return defineTool({
    name: 'doc_chunk',
    description: 'Split document text into overlapping chunks for downstream processing.',
    parameters: z.object({
      text: z.string().describe('Text content to split into chunks'),
    }),
    handler: async ({ text }) => {
      const chunkSize = config.entitySchema?.extraction.chunk_size ?? 1000;
      const chunkOverlap = config.entitySchema?.extraction.chunk_overlap ?? 200;
      return chunkText(text, chunkSize, chunkOverlap);
    },
  });
}

// ---------------------------------------------------------------------------
// createDocExtractEntitiesTool
// ---------------------------------------------------------------------------

/**
 * Create a tool that extracts entities from text chunks using an LLM via FMAPI.
 * Requires `config.entitySchema` to be set.
 */
export function createDocExtractEntitiesTool(config: ConnectorConfig): AgentTool {
  return defineTool({
    name: 'doc_extract_entities',
    description: 'Extract structured entities from document chunks using an LLM.',
    parameters: z.object({
      chunks: z
        .array(
          z.object({
            chunk_id: z.string(),
            text: z.string(),
          }),
        )
        .describe('Array of text chunks to extract entities from'),
      model: z.string().optional().describe('Model to use for extraction (default: databricks-claude-sonnet-4-6)'),
    }),
    handler: async ({ chunks, model }) => {
      const host = resolveHost(config.host);
      const token = await resolveToken();
      const modelName = model ?? 'databricks-claude-sonnet-4-6';

      const schema = config.entitySchema;
      const promptTemplate = schema?.extraction.prompt_template ?? '';
      const entityNames = (schema?.entities ?? []).map((e) => e.name).join(', ');
      const entityFields = (schema?.entities ?? [])
        .flatMap((e) => e.fields.map((f) => f.name))
        .join(', ');

      const allEntities: Array<Record<string, unknown>> = [];

      for (const chunk of chunks) {
        const prompt = promptTemplate
          .replace(/\{entity_names\}/g, entityNames)
          .replace(/\{entity_fields\}/g, entityFields)
          .replace(/\{chunk_text\}/g, chunk.text);

        const url = `${host}/serving-endpoints/chat/completions`;

        const res = await fetch(url, {
          method: 'POST',
          headers: {
            Authorization: `Bearer ${token}`,
            'Content-Type': 'application/json',
          },
          body: JSON.stringify({
            model: modelName,
            messages: [{ role: 'user', content: prompt }],
          }),
        });

        if (!res.ok) {
          const text = await res.text();
          throw new Error(`FMAPI POST ${res.status}: ${text}`);
        }

        const response = (await res.json()) as {
          choices: Array<{ message: { content: string } }>;
        };

        const content = response.choices?.[0]?.message?.content ?? '';

        // Attempt to parse JSON array from LLM response; skip chunk on failure
        try {
          // Strip markdown code fences if present
          const cleaned = content.replace(/^```(?:json)?\s*/i, '').replace(/\s*```$/, '').trim();
          const parsed = JSON.parse(cleaned);
          if (Array.isArray(parsed)) {
            for (const entity of parsed) {
              allEntities.push({ ...entity, _chunk_id: chunk.chunk_id });
            }
          }
        } catch {
          // Non-JSON response: skip this chunk gracefully
        }
      }

      return allEntities;
    },
  });
}
