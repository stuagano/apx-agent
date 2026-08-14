Score each row of this value matrix for ${customer}:
${value_matrices}

For every outcome, score business value and the platform consumption/effort
required to deliver it, each on a 0-10 scale (higher effort = harder).

Return JSON with:
- "cells": array of objects, each with "outcome" (string), "value_score"
  (number), and "effort_score" (number)
