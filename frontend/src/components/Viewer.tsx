/// <reference types="react" />
import React, { useEffect } from "react";
import { DocumentRecord, UploadResponse } from "../types";
import { renderHtml, ensureMathJaxLoaded } from "../utils/helpers";

interface ViewerProps {
  selectedDocument: DocumentRecord | null;
  documents: DocumentRecord[];
  uploadResult: UploadResponse | null;
}

export const Viewer: React.FC<ViewerProps> = ({
  selectedDocument,
  documents,
  uploadResult
}) => {
  const viewerRef = React.useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    async function renderMath() {
      if (!viewerRef.current) return;
      try {
        await ensureMathJaxLoaded();
        const mathJax = (window as any).MathJax;
        if (mathJax?.typesetPromise) {
          await mathJax.typesetPromise([viewerRef.current]);
        }
      } catch (e) {
        // ignore render errors
      }
    }

    renderMath();
  }, [documents, selectedDocument, uploadResult]);

  return (
    <section className="viewer">
      {selectedDocument ? (
        <>
          <div className="viewer-head">
            <div>
              <h2>{selectedDocument.source_file}</h2>
              <p>{selectedDocument.total_questions} parsed questions</p>
            </div>
          </div>

          <div className="question-stack" ref={viewerRef}>
            {selectedDocument.questions.map((question, index) => {
              const previous = selectedDocument.questions[index - 1];
              const compoundHtml = question.compound_html ?? "";
              const compoundText = question.compound_text ?? "";
              const showCompound = compoundHtml && compoundHtml !== (previous?.compound_html ?? "");

              return (
                <article className="question-card" key={question.id}>
                  {showCompound ? (
                    <div
                      className="compound-block"
                      dangerouslySetInnerHTML={renderHtml(compoundHtml, compoundText)}
                    />
                  ) : null}
                  <div className="question-title">
                    <h3>Question {question.number || index + 1}</h3>
                    <span>Answer: {question.answer || "N/A"}</span>
                  </div>
                  <div
                    className="rich-text"
                    dangerouslySetInnerHTML={renderHtml(question.question_html, question.question_text)}
                  />
                  <div className="options">
                    {question.options.map((option) => (
                      <div className="option" key={option.id}>
                        <strong>{option.label}</strong>
                        <span dangerouslySetInnerHTML={renderHtml(option.html, option.text)} />
                      </div>
                    ))}
                  </div>
                  <details>
                    <summary>Solution</summary>
                    <div
                      className="rich-text"
                      dangerouslySetInnerHTML={renderHtml(question.solution_html, question.solution_text || "No solution provided.")}
                    />
                  </details>
                </article>
              );
            })}
          </div>
        </>
      ) : (
        <div className="empty-state">
          <h2>Upload your first DOCX file</h2>
          <p>Parsed documents and questions will appear here.</p>
        </div>
      )}
    </section>
  );
};
