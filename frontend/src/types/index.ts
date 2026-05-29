export type AuthUser = {
  id: number;
  email: string;
};

export type AuthResponse = {
  access_token: string;
  token_type: string;
  user: AuthUser;
};

export type QuestionOption = {
  id: number;
  label: string;
  text: string;
  html: string;
  display_order: number;
};

export type Question = {
  id: number;
  number: string | null;
  question_text: string;
  question_html: string;
  compound_text: string | null;
  compound_html: string | null;
  answer: string | null;
  solution_text: string;
  solution_html: string;
  display_order: number;
  options: QuestionOption[];
};

export type DocumentRecord = {
  id: number;
  source_file: string;
  total_questions: number;
  created_at: string | null;
  questions: Question[];
};

export type DocumentsResponse = {
  total_documents: number;
  total_questions: number;
  documents: DocumentRecord[];
};

export type UploadResponse = {
  total_files: number;
  successful_files: number;
  failed_files: number;
  total_questions: number;
  documents: DocumentRecord[];
  errors: { source_file: string; error: string }[];
};

export type AuthMode = "login" | "signup";
