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

	"github.com/alecthomas/chroma/v2"
	"github.com/alecthomas/chroma/v2/formatters"
	"github.com/alecthomas/chroma/v2/lexers"
	"github.com/alecthomas/chroma/v2/styles"
	sitter "github.com/smacker/go-tree-sitter"
	"github.com/smacker/go-tree-sitter/bash"
	"github.com/smacker/go-tree-sitter/c"
	"github.com/smacker/go-tree-sitter/cpp"
	"github.com/smacker/go-tree-sitter/css"
	"github.com/smacker/go-tree-sitter/golang"
	"github.com/smacker/go-tree-sitter/html"
	"github.com/smacker/go-tree-sitter/javascript"
	"github.com/smacker/go-tree-sitter/php"
	"github.com/smacker/go-tree-sitter/python"
	"github.com/smacker/go-tree-sitter/rust"
	"github.com/smacker/go-tree-sitter/typescript/typescript"
)

type NodeInfo struct {
	Type      string     `json:"type"`
	Name      string     `json:"name"`
	StartLine int        `json:"start_line"`
	EndLine   int        `json:"end_line"`
	Children  []NodeInfo `json:"children,omitempty"`
}

func highlight(filePath string, styleName string, lexerName string) error {
	content, err := os.ReadFile(filePath)
	if err != nil {
		return err
	}

	var lexer chroma.Lexer
	if lexerName != "" {
		lexer = lexers.Get(lexerName)
	}
	if lexer == nil {
		lexer = lexers.Match(filePath)
	}
	if lexer == nil {
		lexer = lexers.Analyse(string(content))
	}
	if lexer == nil {
		lexer = lexers.Fallback
	}

	style := styles.Get(styleName)
	if style == nil {
		style = styles.Fallback
	}

	formatter := formatters.Get("terminal256")
	if formatter == nil {
		formatter = formatters.Fallback
	}

	iterator, err := lexer.Tokenise(nil, string(content))
	if err != nil {
		return err
	}

	return formatter.Format(os.Stdout, style, iterator)
}

func extractNodes(n *sitter.Node, content []byte, lang string) []NodeInfo {
	var nodes []NodeInfo
	for i := 0; i < int(n.NamedChildCount()); i++ {
		child := n.NamedChild(i)
		nodeType := child.Type()

		isDefinition := false
		kind := ""
		name := ""

		switch lang {
		case "python":
			if nodeType == "class_definition" {
				isDefinition = true
				kind = "class"
			} else if nodeType == "function_definition" {
				isDefinition = true
				kind = "function"
			}
		case "go":
			if nodeType == "type_declaration" {
				isDefinition = true
				kind = "type"
			} else if nodeType == "function_declaration" {
				isDefinition = true
				kind = "function"
			} else if nodeType == "method_declaration" {
				isDefinition = true
				kind = "method"
			}
		case "rust":
			if nodeType == "function_item" {
				isDefinition = true
				kind = "function"
			} else if nodeType == "struct_item" {
				isDefinition = true
				kind = "struct"
			} else if nodeType == "enum_item" {
				isDefinition = true
				kind = "enum"
			} else if nodeType == "impl_item" {
				isDefinition = true
				kind = "impl"
				typeNode := child.ChildByFieldName("type")
				if typeNode != nil {
					name = string(content[typeNode.StartByte():typeNode.EndByte()])
				}
			} else if nodeType == "mod_item" {
				isDefinition = true
				kind = "module"
			} else if nodeType == "trait_item" {
				isDefinition = true
				kind = "trait"
			}
		case "c", "cpp":
			if nodeType == "function_definition" {
				isDefinition = true
				kind = "function"
				declarator := child.ChildByFieldName("declarator")
				if declarator != nil {
					for j := 0; j < int(declarator.NamedChildCount()); j++ {
						c := declarator.NamedChild(j)
						if c.Type() == "identifier" || c.Type() == "field_identifier" {
							name = string(content[c.StartByte():c.EndByte()])
							break
						}
					}
				}
			} else if nodeType == "class_specifier" {
				isDefinition = true
				kind = "class"
			} else if nodeType == "struct_specifier" {
				isDefinition = true
				kind = "struct"
			} else if nodeType == "namespace_definition" {
				isDefinition = true
				kind = "namespace"
			}
		case "typescript", "javascript":
			if nodeType == "class_declaration" || nodeType == "class" {
				isDefinition = true
				kind = "class"
			} else if nodeType == "function_declaration" || nodeType == "function" {
				isDefinition = true
				kind = "function"
			} else if nodeType == "method_definition" {
				isDefinition = true
				kind = "method"
			} else if nodeType == "interface_declaration" {
				isDefinition = true
				kind = "interface"
			}
		case "php":
			if nodeType == "class_declaration" {
				isDefinition = true
				kind = "class"
			} else if nodeType == "function_definition" {
				isDefinition = true
				kind = "function"
			} else if nodeType == "method_declaration" {
				isDefinition = true
				kind = "method"
			}
		case "bash":
			if nodeType == "function_definition" {
				isDefinition = true
				kind = "function"
			}
		case "css":
			if nodeType == "rule_set" {
				isDefinition = true
				kind = "rule"
				selector := child.ChildByFieldName("selector")
				if selector != nil {
					name = string(content[selector.StartByte():selector.EndByte()])
				}
			}
		}

		if isDefinition {
			if name == "" {
				nameNode := child.ChildByFieldName("name")
				if nameNode == nil && lang == "go" && nodeType == "type_declaration" {
					spec := child.NamedChild(0)
					if spec != nil && spec.Type() == "type_spec" {
						nameNode = spec.ChildByFieldName("name")
					}
				}
				if nameNode != nil {
					name = string(content[nameNode.StartByte():nameNode.EndByte()])
				} else {
					name = "Unknown"
				}
			}

			var children []NodeInfo
			bodyNode := child.ChildByFieldName("body")
			if bodyNode == nil {
				if lang == "go" {
					for j := 0; j < int(child.NamedChildCount()); j++ {
						c := child.NamedChild(j)
						if c.Type() == "block" {
							bodyNode = c
							break
						}
					}
				} else if lang == "css" && nodeType == "rule_set" {
					bodyNode = child.ChildByFieldName("block")
				} else if lang == "rust" {
					for j := 0; j < int(child.NamedChildCount()); j++ {
						c := child.NamedChild(j)
						if c.Type() == "block" || c.Type() == "declaration_list" || c.Type() == "enum_variant_list" {
							bodyNode = c
							break
						}
					}
				} else if lang == "c" || lang == "cpp" {
					bodyNode = child.ChildByFieldName("body")
					if bodyNode == nil {
						for j := 0; j < int(child.NamedChildCount()); j++ {
							c := child.NamedChild(j)
							if c.Type() == "compound_statement" || c.Type() == "field_declaration_list" {
								bodyNode = c
								break
							}
						}
					}
				} else if lang == "php" {
					for j := 0; j < int(child.NamedChildCount()); j++ {
						c := child.NamedChild(j)
						if c.Type() == "declaration_list" || c.Type() == "compound_statement" {
							bodyNode = c
							break
						}
					}
				}
			}

			if bodyNode != nil {
				children = extractNodes(bodyNode, content, lang)
			}

			nodes = append(nodes, NodeInfo{
				Type:      kind,
				Name:      name,
				StartLine: int(child.StartPoint().Row) + 1,
				EndLine:   int(child.EndPoint().Row) + 1,
				Children:  children,
			})
		} else {
			if nodeType == "translation_unit" || nodeType == "source_file" || nodeType == "module" || 
			   nodeType == "script" || nodeType == "program" || nodeType == "class_body" || 
			   nodeType == "block" || nodeType == "decorated_definition" || nodeType == "namespace_definition" ||
			   nodeType == "php_tag" || nodeType == "namespace_use_declaration" {
				nodes = append(nodes, extractNodes(child, content, lang)...)
			}
		}
	}
	return nodes
}

func parse(filePath string) error {
	content, err := os.ReadFile(filePath)
	if err != nil {
		return err
	}

	ext := strings.ToLower(filepath.Ext(filePath))
	var lang *sitter.Language
	langName := ""

	switch ext {
	case ".py":
		lang = python.GetLanguage()
		langName = "python"
	case ".go":
		lang = golang.GetLanguage()
		langName = "go"
	case ".ts", ".tsx":
		lang = typescript.GetLanguage()
		langName = "typescript"
	case ".js", ".jsx":
		lang = javascript.GetLanguage()
		langName = "javascript"
	case ".sh", ".bash":
		lang = bash.GetLanguage()
		langName = "bash"
	case ".css":
		lang = css.GetLanguage()
		langName = "css"
	case ".c", ".h":
		lang = c.GetLanguage()
		langName = "c"
	case ".cpp", ".hpp", ".cc", ".cxx":
		lang = cpp.GetLanguage()
		langName = "cpp"
	case ".rs":
		lang = rust.GetLanguage()
		langName = "rust"
	case ".html", ".htm":
		lang = html.GetLanguage()
		langName = "html"
	case ".php":
		lang = php.GetLanguage()
		langName = "php"
	default:
		return fmt.Errorf("unsupported file extension: %s", ext)
	}

	parser := sitter.NewParser()
	parser.SetLanguage(lang)

	tree, err := parser.ParseCtx(context.Background(), nil, content)
	if err != nil {
		return err
	}

	root := tree.RootNode()
	nodes := extractNodes(root, content, langName)

	output, err := json.MarshalIndent(nodes, "", "  ")
	if err != nil {
		return err
	}

	fmt.Println(string(output))
	return nil
}

func dump(filePath string) error {
	content, err := os.ReadFile(filePath)
	if err != nil {
		return err
	}

	ext := strings.ToLower(filepath.Ext(filePath))
	var lang *sitter.Language
	switch ext {
	case ".py": lang = python.GetLanguage()
	case ".go": lang = golang.GetLanguage()
	case ".php": lang = php.GetLanguage()
	default: return fmt.Errorf("unsupported: %s", ext)
	}

	parser := sitter.NewParser()
	parser.SetLanguage(lang)
	tree, _ := parser.ParseCtx(context.Background(), nil, content)

	var printNode func(*sitter.Node, string)
	printNode = func(n *sitter.Node, indent string) {
		fmt.Printf("%s%s [%d-%d]\n", indent, n.Type(), n.StartPoint().Row, n.EndPoint().Row)
		for i := 0; i < int(n.NamedChildCount()); i++ {
			printNode(n.NamedChild(i), indent+"  ")
		}
	}
	printNode(tree.RootNode(), "")
	return nil
}

func main() {
	if len(os.Args) < 2 {
		fmt.Println("Usage: federate_bridge <command> <args...>")
		fmt.Println("Commands: highlight, parse, dump")
		os.Exit(1)
	}

	command := os.Args[1]
	switch command {
	case "dump":
		if len(os.Args) < 3 {
			fmt.Println("Usage: federate_bridge dump <file_path>")
			os.Exit(1)
		}
		err := dump(os.Args[2])
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error: %v\n", err)
			os.Exit(1)
		}
	case "highlight":
		if len(os.Args) < 3 {
			fmt.Println("Usage: federate_bridge highlight <file_path> [style] [lexer]")
			os.Exit(1)
		}
		style := "monokai"
		if len(os.Args) >= 4 {
			style = os.Args[3]
		}
		lexer := ""
		if len(os.Args) >= 5 {
			lexer = os.Args[4]
		}
		err := highlight(os.Args[2], style, lexer)
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error highlighting file: %v\n", err)
			os.Exit(1)
		}
	case "parse":
		if len(os.Args) < 3 {
			fmt.Println("Usage: federate_bridge parse <file_path>")
			os.Exit(1)
		}
		err := parse(os.Args[2])
		if err != nil {
			fmt.Fprintf(os.Stderr, "Error parsing file: %v\n", err)
			os.Exit(1)
		}
	default:
		fmt.Printf("Unknown command: %s\n", command)
		os.Exit(1)
	}
}
