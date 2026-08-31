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

func main() {
	if len(os.Args) < 2 {
		fmt.Fprintln(os.Stderr, "Usage: federate_embed <text_to_embed>")
		os.Exit(1)
	}

	inputText := strings.Join(os.Args[1:], " ")
	ctx := context.Background()

	// 1. Tokenize into sentences
	tokenizer, err := english.NewSentenceTokenizer(nil)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error creating tokenizer: %v\n", err)
		os.Exit(1)
	}
	sentences := tokenizer.Tokenize(inputText)

	// 2. Load Model
	modelName := "sentence-transformers/all-MiniLM-L6-v2"
	
	homeDir, err := os.UserHomeDir()
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error getting home directory: %v\n", err)
		os.Exit(1)
	}
	modelsDir := filepath.Join(homeDir, ".federate", "models")
	
	// Create the global models directory if it doesn't exist
	err = os.MkdirAll(modelsDir, 0755)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Error creating models directory: %v\n", err)
		os.Exit(1)
	}

	conf := &tasks.Config{
		ModelsDir:      modelsDir,
		ModelName:      modelName,
		DownloadPolicy: tasks.DownloadMissing,
	}

	obj, err := tasks.Load[textencoding.Interface](conf)
	if err != nil {
		fmt.Fprintf(os.Stderr, "Failed to load model: %v\n", err)
		os.Exit(1)
	}

	var results []EmbeddingResult
	for _, s := range sentences {
		trimmed := strings.TrimSpace(s.Text)
		if trimmed == "" {
			continue
		}

		res, err := obj.Encode(ctx, trimmed, 0)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Failed to encode sentence: %v\n", err)
			continue
		}

		results = append(results, EmbeddingResult{
			Text:   trimmed,
			Vector: res.Vector.Data().F64(),
		})
	}

	// 3. Output as JSON
	json.NewEncoder(os.Stdout).Encode(results)
}
