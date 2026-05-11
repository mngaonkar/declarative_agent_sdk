/*
 * Copyright 2025 Google LLC
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *      https://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

import { SignalWatcher } from "@lit-labs/signals";
import { provide } from "@lit/context";
import {
  LitElement,
  html,
  css,
  nothing,
  HTMLTemplateResult,
  unsafeCSS,
} from "lit";
import { customElement, state } from "lit/decorators.js";
import { theme as uiTheme } from "./theme/theme.js";
import { A2UIClient } from "./client.js";
import {
  SnackbarAction,
  SnackbarMessage,
  SnackbarUUID,
  SnackType,
} from "./types/types.js";
import { type Snackbar } from "./ui/snackbar.js";
import { repeat } from "lit/directives/repeat.js";
import { v0_8 } from "@a2ui/lit";
import * as UI from "@a2ui/lit/ui";

// Demo elements.
import "./ui/ui.js";
import { registerContactComponents } from "./ui/custom-components/register-components.js";
import { Context } from "@a2ui/lit/ui";
// @ts-ignore
import { renderMarkdown } from "@a2ui/markdown-it";

// Register custom components for the A2UI app
registerContactComponents();

@customElement("a2ui-main")
export class A2UIMain extends SignalWatcher(LitElement) {
  connectedCallback() {
    super.connectedCallback();
  }

  @provide({ context: UI.Context.themeContext })
  accessor theme: v0_8.Types.Theme = uiTheme;

  @provide({ context: UI.Context.markdown })
  accessor markdownRenderer: v0_8.Types.MarkdownRenderer = async (text, options) => {
    return renderMarkdown(text, options);
  };

  @state()
  accessor #requesting = false;

  @state()
  accessor #error: string | null = null;

  @state()
  accessor renderVersion = 0;

  @state()
  accessor #lastMessages: v0_8.Types.ServerToClientMessage[] = [];

  static styles = [
    unsafeCSS(v0_8.Styles.structuralStyles),
    css`
      :host {
        display: flex;
        flex-direction: column;
        width: 100%;
        margin: 0 auto;
        height: 100vh;
        overflow: hidden;
      }

      #chat-container {
        display: flex;
        flex-direction: column;
        height: 100%;
        overflow: hidden;
      }

      #messages-area {
        flex: 1;
        overflow-y: auto;
        overflow-x: hidden;
        padding: 16px;
        display: flex;
        flex-direction: column;
        gap: 16px;
      }

      #surfaces {
        display: flex;
        flex-direction: column;
        gap: 16px;
        width: 100%;
        animation: fadeIn 1s cubic-bezier(0, 0, 0.3, 1) 0.3s backwards;

        & a2ui-surface {
          width: 100%;
        }
      }

      #input-area {
        flex-shrink: 0;
        border-top: 1px solid var(--p-90);
        background: var(--n-100);
        padding: 16px;
        box-shadow: 0 -2px 8px rgba(0, 0, 0, 0.05);
      }

      form {
        display: flex;
        flex-direction: column;
        gap: 8px;
        width: 100%;

        & h1 {
          margin: 0 0 8px 0;
          font-size: 14px;
          font-weight: 400;
          color: var(--p-30);
        }

        & > div {
          display: flex;
          gap: 12px;
          align-items: center;
          width: 100%;

          & > input {
            display: block;
            flex: 1;
            border-radius: 24px;
            padding: 12px 20px;
            border: 1px solid var(--p-80);
            font-size: 15px;
            outline: none;
            transition: border-color 0.2s;

            &:focus {
              border-color: var(--p-40);
            }
          }

          & > button {
            display: flex;
            align-items: center;
            justify-content: center;
            background: var(--p-40);
            color: var(--n-100);
            border: none;
            padding: 12px;
            border-radius: 50%;
            width: 44px;
            height: 44px;
            opacity: 0.5;
            transition: all 0.2s;

            &:not([disabled]) {
              cursor: pointer;
              opacity: 1;

              &:hover {
                background: var(--p-30);
                transform: scale(1.05);
              }
            }
          }
        }
      }

      .rotate {
        animation: spin 1s linear infinite;
      }

      .pending {
        width: 100%;
        min-height: 200px;
        display: flex;
        align-items: center;
        justify-content: center;
        animation: fadeIn 1s cubic-bezier(0, 0, 0.3, 1) 0.3s backwards;

        & .g-icon {
          margin-right: 8px;
        }
      }

      .error {
        color: var(--e-40);
        background-color: var(--e-95);
        border: 1px solid var(--e-80);
        padding: 16px;
        border-radius: 8px;
      }

      @keyframes fadeIn {
        from {
          opacity: 0;
        }

        to {
          opacity: 1;
        }
      }

      .spinner {
        width: 48px;
        height: 48px;
        border: 4px solid rgba(255, 255, 255, 0.1);
        border-left-color: var(--p-60);
        border-radius: 50%;
        animation: spin 1s linear infinite;
      }

      @keyframes spin {
        to {
          transform: rotate(360deg);
        }
      }

      @keyframes pulse {
        0% {
          opacity: 0.6;
        }
        50% {
          opacity: 1;
        }
        100% {
          opacity: 0.6;
        }
      }
    `,
  ];

  #processor = v0_8.Data.createSignalA2uiMessageProcessor();
  #a2uiClient = new A2UIClient();
  #snackbar: Snackbar | undefined = undefined;
  #pendingSnackbarMessages: Array<{
    message: SnackbarMessage;
    replaceAll: boolean;
  }> = [];
  #messagesArea: HTMLElement | null = null;

  render() {
    return html`
      <div id="chat-container">
        <div id="messages-area" ${(el: Element) => {
          this.#messagesArea = el as HTMLElement;
          this.#scrollToBottom();
        }}>
          ${this.#maybeRenderData()}
          ${this.#maybeRenderError()}
        </div>
        <div id="input-area">
          ${this.#renderInputForm()}
        </div>
      </div>
    `;
  }

  #scrollToBottom() {
    requestAnimationFrame(() => {
      if (this.#messagesArea) {
        this.#messagesArea.scrollTop = this.#messagesArea.scrollHeight;
      }
    });
  }

  #maybeRenderError() {
    if (!this.#error) return nothing;

    return html`<div class="error">${this.#error}</div>`;
  }

  #renderInputForm() {
    return html`<form
      @submit=${async (evt: Event) => {
        evt.preventDefault();
        if (!(evt.target instanceof HTMLFormElement)) {
          return;
        }
        const data = new FormData(evt.target);
        const body = data.get("body") ?? null;
        if (!body) {
          return;
        }
      const message: v0_8.Types.A2UIClientEventMessage = {
        request: body,
      };
        await this.#sendAndProcessMessage(message);
        // Clear the input after sending
        evt.target.reset();
      }}
    >
      <div>
        <input
          required
          value=""
          placeholder="Type your message..."
          autocomplete="off"
          id="body"
          name="body"
          type="text"
          ?disabled=${this.#requesting}
        />
        <button type="submit" ?disabled=${this.#requesting}>
          <span class="g-icon filled-heavy">send</span>
        </button>
      </div>
    </form>`;
  }

  #maybeRenderData() {
    const surfacesMap = new Map(this.#processor.getSurfaces());
    const surfaces = Array.from(surfacesMap.entries()).sort(([a], [b]) => {
      return a.localeCompare(b);
    });

    if (this.#requesting && surfaces.length === 0) {
      return html` <div class="pending">
        <div class="spinner"></div>
        <div class="loading-text">Awaiting an answer...</div>
      </div>`;
    }

    if (surfaces.length === 0) {
      return html`<div style="flex: 1; display: flex; align-items: center; justify-content: center; color: var(--p-60); font-size: 14px;">
        Start a conversation by typing a message below
      </div>`;
    }

    return html`<section id="surfaces">
      ${repeat(
        surfaces,
        ([surfaceId]) => surfaceId,
        ([surfaceId, surface]) => {
          return html`
            <div style="position: relative; display: flex; flex-direction: column; background: white; border-radius: 8px; padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);">
              ${this.#requesting ? html`
                <div style="
                  position: absolute;
                  top: 0;
                  left: 0;
                  right: 0;
                  bottom: 0;
                  background: rgba(255, 255, 255, 0.7);
                  z-index: 10;
                  display: flex;
                  align-items: center;
                  justify-content: center;
                  backdrop-filter: blur(2px);
                  border-radius: 8px;
                ">
                  <span class="g-icon filled-heavy rotate" style="font-size: 32px; color: var(--p-40);">progress_activity</span>
                </div>
              ` : nothing}
                <a2ui-surface
                  .surface=${{ ...surface }}
                  @a2uiaction=${async (
            evt: v0_8.Events.StateEvent<"a2ui.action">
          ) => {
              const [target] = evt.composedPath();
              if (!(target instanceof HTMLElement)) {
                return;
              }

            const context: v0_8.Types.A2UIClientEventMessage["userAction"]["context"] =
              {};
            if (evt.detail.action.context) {
              const srcContext = evt.detail.action.context;
              for (const item of srcContext) {
                if (item.value.literalBoolean) {
                  context[item.key] = item.value.literalBoolean;
                } else if (item.value.literalNumber) {
                  context[item.key] = item.value.literalNumber;
                } else if (item.value.literalString) {
                  context[item.key] = item.value.literalString;
                } else if (item.value.path) {
                  const path = this.#processor.resolvePath(
                    item.value.path,
                    evt.detail.dataContextPath
                  );
                  const value = this.#processor.getData(
                    evt.detail.sourceComponent,
                    path,
                    surfaceId
                  );
                  context[item.key] = value;
                }
              }
            }

            const message: v0_8.Types.A2UIClientEventMessage = {
              userAction: {
                surfaceId: surfaceId,
                name: "ACTION: " + evt.detail.action.name,
                sourceComponentId: target.id,
                timestamp: new Date().toISOString(),
                context,
              },
            };



              await this.#sendAndProcessMessage(message);
            }}
                .surfaceId=${surfaceId}

                .processor=${this.#processor}
                .enableCustomElements=${true}
              ></a2ui-surface>
            </div>`;
        }
      )}
    </section>`;
  }

  async #sendAndProcessMessage(request: v0_8.Types.A2UIClientEventMessage) {
    this.#requesting = true;
    const messages = await this.#sendMessage(request);

    console.log("About to process messages:", messages);
    this.#lastMessages = messages;

    // this.#processor.clearSurfaces(); // Removed to allow partial updates
    this.#processor.processMessages(messages);
    console.log("After processing, surfaces:", Array.from(this.#processor.getSurfaces().keys()));
    console.log("Surface data:", Array.from(this.#processor.getSurfaces().entries()));
    
    this.renderVersion++; // Force re-render of surfaces
    this.requestUpdate();
    this.#scrollToBottom();

    const cardSurface = this.#processor.getSurfaces().get('contact-card');
    if (cardSurface) {
      console.log("Card surface found:", cardSurface);
      // Deeply log the data model for the card
    } else {
      console.log("No contact-card surface found!");
    }
  }

  async #sendMessage(
    message: v0_8.Types.A2UIClientEventMessage
  ): Promise<v0_8.Types.ServerToClientMessage[]> {
    try {
      this.#requesting = true;
      const response = await this.#a2uiClient.send(message);

      this.#requesting = false;

      return response;
    } catch (err) {
      this.snackbar(err as string, SnackType.ERROR);
    } finally {
      this.#requesting = false;
    }

    return [];
  }

  snackbar(
    message: string | HTMLTemplateResult,
    type: SnackType,
    actions: SnackbarAction[] = [],
    persistent = false,
    id = globalThis.crypto.randomUUID(),
    replaceAll = false
  ) {
    if (!this.#snackbar) {
      this.#pendingSnackbarMessages.push({
        message: {
          id,
          message,
          type,
          persistent,
          actions,
        },
        replaceAll,
      });
      return;
    }

    return this.#snackbar.show(
      {
        id,
        message,
        type,
        persistent,
        actions,
      },
      replaceAll
    );
  }

  unsnackbar(id?: SnackbarUUID) {
    if (!this.#snackbar) {
      return;
    }

    this.#snackbar.hide(id);
  }
}
