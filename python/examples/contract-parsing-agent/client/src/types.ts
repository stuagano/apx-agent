export interface Contract {
  contract_id: string
  counterparty: string
  contract_type: string
  effective_date: string | null
  expiry_date: string | null
  term_years: number | null
  auto_renewal: boolean | null
}

export interface Message {
  role: 'user' | 'assistant'
  content: string
}
