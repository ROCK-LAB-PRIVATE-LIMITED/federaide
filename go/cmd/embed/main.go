/*
FEDERaiDE is a multi-agent multi-modal automation and orchestration harness.
Copyright (C) 2026  ROCK LAB PRIVATE LIMITED

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU Affero General Public License as published
by the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU Affero General Public License for more details.

You should have received a copy of the GNU Affero General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
*/

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strings"

	"github.com/neurosnap/sentences/english"
	"github.com/nlpodyssey/cybertron/pkg/tasks"
	"github.com/nlpodyssey/cybertron/pkg/tasks/textencoding"
)

type EmbeddingResult struct {
	Text   string    `json:"text"`
	Vector []float64 `json:"vector"`
}

// patchTokenizerConfig flattens HuggingFace object-tokens back into standard strings
func patchTokenizerConfig(modelsDir, modelName string) {
	configPath := filepath.Join(modelsDir, modelName, "tokenizer_config.json")
	data, err := os.ReadFile(configPath)
	if err != nil {
		return
	}

	var config map[string]interface{}
	if err := json.Unmarshal(data, &config); err != nil {
		return
	}

	changed := false
	tokensToCheck := []string{"mask_token", "unk_token", "sep_token", "pad_token", "cls_token", "bos_token", "eos_token"}
	for _, tok := range tokensToCheck {
		if val, ok := config[tok].(map[string]interface{}); ok {
			if content, ok := val["content"].(string); ok {
				config[tok] = content
				changed = true
			}
		}
	}

	if changed {
		if newData, err := json.MarshalIndent(config, "", "  "); err == nil {
			os.WriteFile(configPath, newData, 0644)
		}
	}
}

func main() {
	if len(os.Args) < 3 {
		fmt.Fprintln(os.Stderr, "Usage: federate_embed <model_name> <text_to_embed...>")
		os.Exit(1)
	}

	modelName := os.Args[1]
	inputText := strings.Join(os.Args[2:], " ")
	ctx := context.Background()

	// 1. Tokenize into sentences
	tokenizer, err := english.NewSentenceTokenizer(nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error creating tokenizer: %v\n", err)
		os.Exit(1)
	}
	sentences := tokenizer.Tokenize(inputText)

	// 2. Load Model
	
	homeDir, err := os.UserHomeDir()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error getting home directory: %v\n", err)
		os.Exit(1)
	}
	modelsDir := filepath.Join(homeDir, ".federaide", "models")
	
	// Create the global models directory if it doesn't exist
	err = os.MkdirAll(modelsDir, 0755)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error creating models directory: %v\n", err)
		os.Exit(1)
	}

	conf := &tasks.Config{
		ModelsDir:        modelsDir,
		ModelName:        modelName,
		DownloadPolicy:   tasks.DownloadMissing,
		ConversionPolicy: tasks.ConvertMissing,
	}

	obj, err := tasks.Load[textencoding.Interface](conf)
	if err != nil {
		// Try to patch HuggingFace tokenizer config anomalies and retry
		patchTokenizerConfig(modelsDir, modelName)
		obj, err = tasks.Load[textencoding.Interface](conf)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to load model: %v\n", err)
			os.Exit(1)
		}
	}

	results := make([]EmbeddingResult, 0)
	for _, s := range sentences {
		trimmed := strings.TrimSpace(s.Text)
		if trimmed == "" {
			continue
		}

		err = func() (err error) {
			defer func() {
				if r := recover(); r != nil {
					err = fmt.Errorf("panic during encoding: %v", r)
				}
			}()
			res, err := obj.Encode(ctx, trimmed, 0)
			if err != nil {
				return err
			}
			results = append(results, EmbeddingResult{
				Text:   trimmed,
				Vector: res.Vector.Data().F64(),
			})
			return nil
		}()

		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to encode sentence: %v\n", err)
			continue
		}
	}

	// 3. Output as JSON
	json.NewEncoder(os.Stdout).Encode(results)
}
