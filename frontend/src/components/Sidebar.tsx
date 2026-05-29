/// <reference types="react" />
import React, { FormEvent, ChangeEvent } from "react";
import { DocumentRecord, UploadResponse } from "../types";
import { readJson } from "../utils/helpers";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "";

interface SidebarProps {
  token: string;
  isBusy: boolean;
  setIsBusy: (busy: boolean) => void;
  isDeleting: boolean;
  setIsDeleting: (deleting: boolean) => void;
  message: string;
  setMessage: (msg: string) => void;
  uploadResult: UploadResponse | null;
  setUploadResult: (result: UploadResponse | null) => void;
  documents: DocumentRecord[];
  setDocuments: React.Dispatch<React.SetStateAction<DocumentRecord[]>>;
  selectedDocumentId: number | null;
  setSelectedDocumentId: React.Dispatch<React.SetStateAction<number | null>>;
  loadDocuments: () => Promise<void>;
}

export const Sidebar: React.FC<SidebarProps> = ({
  token,
  isBusy,
  setIsBusy,
  isDeleting,
  setIsDeleting,
  message,
  setMessage,
  uploadResult,
  setUploadResult,
  documents,
  setDocuments,
  selectedDocumentId,
  setSelectedDocumentId,
  loadDocuments
}) => {
  const [selectedFiles, setSelectedFiles] = React.useState<FileList | null>(null);

  async function handleUpload(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!selectedFiles?.length) {
      setMessage("Choose at least one DOCX file.");
      return;
    }

    const formData = new FormData();
    Array.from(selectedFiles).forEach((file) => formData.append("files", file));

    setIsBusy(true);
    setMessage("");
    setUploadResult(null);

    try {
      const result = await readJson<UploadResponse>(
        await fetch(`${API_BASE}/api/v1/parse`, {
          method: "POST",
          headers: { Authorization: `Bearer ${token}` },
          body: formData
        })
      );
      setUploadResult(result);
      setMessage(`Imported ${result.successful_files} of ${result.total_files} files.`);
      await loadDocuments();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed");
    } finally {
      setIsBusy(false);
    }
  }

  async function handleDeleteDocument(document: DocumentRecord) {
    const confirmed = window.confirm(
      `Delete ${document.source_file}? This cannot be undone.`
    );
    if (!confirmed) return;

    setIsDeleting(true);
    setMessage("");

    try {
      await readJson<{ ok: boolean }>(
        await fetch(`${API_BASE}/api/v1/documents/${document.id}`, {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` }
        })
      );

      setDocuments((current) => {
        const remaining = current.filter((doc) => doc.id !== document.id);
        setSelectedDocumentId((currentId) => {
          if (currentId !== document.id) return currentId;
          return remaining[0]?.id ?? null;
        });
        return remaining;
      });
      setMessage("Document deleted.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Failed to delete document");
    } finally {
      setIsDeleting(false);
    }
  }

  return (
    <aside className="sidebar">
      <form className="upload-box" onSubmit={handleUpload}>
        <label>
          Upload DOCX files
          <input
            type="file"
            accept=".docx,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            multiple
            onChange={(event: ChangeEvent<HTMLInputElement>) => setSelectedFiles(event.target.files)}
          />
        </label>
        <button className="primary-button" disabled={isBusy} type="submit">
          {isBusy ? "Uploading..." : "Parse upload"}
        </button>
      </form>

      {message && <p className="status-message">{message}</p>}
      {uploadResult?.errors.length ? (
        <div className="error-list">
          {uploadResult.errors.map((error) => (
            <p key={error.source_file}>
              <strong>{error.source_file}</strong>: {error.error}
            </p>
          ))}
        </div>
      ) : null}

      <div className="document-list">
        <h2>Saved documents</h2>
        {documents.length === 0 ? (
          <p className="muted">No documents yet.</p>
        ) : (
          documents.map((document) => (
            <div
              key={document.id}
              className={selectedDocumentId === document.id ? "doc-row active" : "doc-row"}
              role="button"
              tabIndex={0}
              onClick={() => setSelectedDocumentId(document.id)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  setSelectedDocumentId(document.id);
                }
              }}
            >
              <div className="doc-meta">
                <span>{document.source_file}</span>
                <small>{document.total_questions} questions</small>
              </div>
              <button
                type="button"
                className="doc-delete"
                disabled={isDeleting}
                onClick={(event) => {
                  event.stopPropagation();
                  handleDeleteDocument(document);
                }}
              >
                Delete
              </button>
            </div>
          ))
        )}
      </div>
    </aside>
  );
};
