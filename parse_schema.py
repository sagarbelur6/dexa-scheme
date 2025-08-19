#!/usr/bin/env python3
import argparse
import json
import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple


# Configure module-level logger
logger = logging.getLogger("schema_parser")


@dataclass
class SegmentOccurrence:
	name: str  # base name without suffix like ':2'
	full_name: str  # original name as in diagram (may include suffix)
	order_index: int  # 1-based index within ST subtree for numbering


@dataclass
class ParsedInputRecordDetails:
	# Map of segment base name -> ordered list of element code strings (top-level only)
	segment_to_elements: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class ParsedOutputRecordDetails:
	# Map of element base name -> ordered list of friendly attribute names
	element_to_attributes: Dict[str, List[str]] = field(default_factory=dict)


@dataclass
class ParsedOutputBranching:
	# Map of element base name -> tuple(min_use, max_use)
	element_cardinality: Dict[str, Tuple[int, int]] = field(default_factory=dict)


class SchemaParser:
	def __init__(self, raw_text: str) -> None:
		self.text = raw_text
		self.lines = raw_text.splitlines()
		self.num_lines = len(self.lines)

	def _find_section_bounds(self, start_marker: str, end_marker: Optional[str]) -> Tuple[int, int]:
		start_idx = -1
		end_idx = self.num_lines
		for idx, line in enumerate(self.lines):
			if start_marker in line:
				start_idx = idx
				break
		if start_idx == -1:
			raise ValueError(f"Section start marker not found: {start_marker}")
		if end_marker is not None:
			for idx in range(start_idx + 1, self.num_lines):
				if end_marker in self.lines[idx]:
					end_idx = idx
					break
		return start_idx, end_idx

	def parse_input_branching_segments(self) -> List[SegmentOccurrence]:
		start, end = self._find_section_bounds("INPUT Branching Diagram", "OUTPUT Branching Diagram")
		occurrences: List[SegmentOccurrence] = []

		segment_line_re = re.compile(r"^\s*Segment\s+([A-Za-z0-9_:]+)\*")

		inside_st_block = False
		st_order_counter = 0

		for raw_line in self.lines[start:end]:
			line = raw_line.rstrip("\n")
			m = segment_line_re.match(line)
			if not m:
				continue

			seg_name_with_suffix = m.group(1)
			base_name = seg_name_with_suffix.split(":")[0]

			if base_name == "ST":
				inside_st_block = True
				st_order_counter = 0
				continue

			if not inside_st_block:
				continue

			if base_name == "SE":
				inside_st_block = False
				break

			if base_name.startswith("Temp_"):
				continue

			st_order_counter += 1
			occurrences.append(
				SegmentOccurrence(name=base_name, full_name=seg_name_with_suffix, order_index=st_order_counter)
			)

		logger.info("Parsed %d segment occurrences under ST", len(occurrences))
		return occurrences

	def parse_input_record_details(self) -> ParsedInputRecordDetails:
		start, end = self._find_section_bounds("INPUT Record Details", "OUTPUT Record Details")
		# Patterns
		segment_hdr_re = re.compile(r"^\s*Segment\s+([A-Z0-9:]+)\*")
		# Top-level numeric element code (e.g., 0128* or 0140:2*)
		element_code_re = re.compile(r"^\s*(\d{3,4})(?::\d+)?\*\s+")
		# Composite header line inside segment details (e.g., C040*)
		composite_hdr_re = re.compile(r"^\s*[A-Z][A-Z0-9]{2,}\*\s*$")

		segment_to_elements: Dict[str, List[str]] = {}
		current_segment_base: Optional[str] = None
		skipping_composite = False

		for raw_line in self.lines[start:end]:
			line = raw_line.rstrip("\n")
			m_hdr = segment_hdr_re.match(line)
			if m_hdr:
				seg_name = m_hdr.group(1)
				current_segment_base = seg_name.split(":")[0]
				segment_to_elements.setdefault(current_segment_base, [])
				skipping_composite = False
				continue

			if current_segment_base is None:
				continue

			if skipping_composite and not line.strip():
				skipping_composite = False
				continue

			# Detect start of composite and skip its inner numeric lines
			if composite_hdr_re.match(line.strip()):
				if not line.strip().startswith("Segment") and not element_code_re.match(line):
					skipping_composite = True
					continue

			if skipping_composite:
				continue

			m_elem = element_code_re.match(line)
			if m_elem:
				code = m_elem.group(1)
				segment_to_elements[current_segment_base].append(code)

		# Deduplicate while preserving order per segment
		for seg_name, elements in list(segment_to_elements.items()):
			seen: set = set()
			deduped: List[str] = []
			for code in elements:
				if code not in seen:
					deduped.append(code)
					seen.add(code)
			segment_to_elements[seg_name] = deduped

		logger.info("Parsed input record details for %d segments", len(segment_to_elements))
		return ParsedInputRecordDetails(segment_to_elements=segment_to_elements)

	def parse_output_branching(self) -> ParsedOutputBranching:
		start, end = self._find_section_bounds("OUTPUT Branching Diagram", "OUTPUT Record Details")

		element_re = re.compile(
			r"^(?P<indent>\s*)Element\s+(?P<name>[A-Za-z0-9:]+)\*\s+[MC]\s+(?P<min>\d+)\s+(?P<max>\d+)"
		)

		element_cardinality: Dict[str, Tuple[int, int]] = {}

		st_indent: Optional[int] = None
		inside_st = False
		indent_stack: List[int] = []

		for raw_line in self.lines[start:end]:
			line = raw_line.rstrip("\n")
			m = element_re.match(line)
			if not m:
				continue
			indent = len(m.group("indent"))
			name = m.group("name")
			min_use = int(m.group("min"))
			max_use = int(m.group("max"))

			# Maintain a basic indent stack to detect leaving ST
			while indent_stack and indent_stack[-1] >= indent:
				popped = indent_stack.pop()
				if inside_st and st_indent is not None and popped == st_indent:
					inside_st = False
					st_indent = None

			indent_stack.append(indent)

			base_name = name.split(":")[0]
			if base_name.lower().startswith("loop"):
				# container, skip
				continue

			if base_name == "ST":
				inside_st = True
				st_indent = indent
				continue

			if not inside_st:
				continue

			# Skip container-like entries mistakenly captured
			if base_name in {"ControlSegment", "ISA", "GS", "GE", "IEA"}:
				# We keep only elements inside ST content for xml.json
				continue

			prior = element_cardinality.get(base_name)
			if prior is None:
				element_cardinality[base_name] = (min_use, max_use)
			else:
				# If any occurrence allows many, reflect that
				combined_min = min(prior[0], min_use)
				combined_max = max(prior[1], max_use)
				element_cardinality[base_name] = (combined_min, combined_max)

		logger.info("Parsed output branching for %d elements under ST", len(element_cardinality))
		return ParsedOutputBranching(element_cardinality=element_cardinality)

	def parse_output_record_details(self) -> ParsedOutputRecordDetails:
		start, end = self._find_section_bounds("OUTPUT Record Details", None)

		attr_hdr_re = re.compile(r"^\s*Attribute\s+([A-Za-z0-9:]+)\*")
		# First line of an attribute (truncated internal name) ends with * and fields
		# e.g., "cityName*" followed by next line containing the friendly name then CDATA
		first_line_field_re = re.compile(r"^\s*([A-Za-z0-9:]+)\*\s+")
		friendly_line_re = re.compile(r"^\s+([A-Za-z0-9][A-Za-z0-9]*)\s+CDATA\s+")

		element_to_attributes: Dict[str, List[str]] = {}
		current_element_base: Optional[str] = None
		awaiting_friendly_name = False

		for raw_line in self.lines[start:end]:
			line = raw_line.rstrip("\n")

			m_hdr = attr_hdr_re.match(line)
			if m_hdr:
				name = m_hdr.group(1)
				base = name.split(":")[0]
				current_element_base = base
				element_to_attributes.setdefault(base, [])
				awaiting_friendly_name = False
				continue

			if current_element_base is None:
				continue

			# Match potential first line (internal) indicating a new attribute block
			m_field = first_line_field_re.match(line)
			if m_field:
				awaiting_friendly_name = True
				continue

			if awaiting_friendly_name:
				mf = friendly_line_re.match(line)
				if mf:
					friendly = mf.group(1)
					# Only add if not already present
					attrs = element_to_attributes[current_element_base]
					if friendly not in attrs:
						attrs.append(friendly)
					awaiting_friendly_name = False
				# If it did not match, remain awaiting until a friendly line is found or header changes

		logger.info("Parsed output record details for %d elements", len(element_to_attributes))
		return ParsedOutputRecordDetails(element_to_attributes=element_to_attributes)


def build_edi_json(
	occurrences: List[SegmentOccurrence],
	input_details: ParsedInputRecordDetails,
) -> Dict[str, Dict[str, Dict[str, str]]]:
	result: Dict[str, Dict[str, Dict[str, str]]] = {}
	for occ in occurrences:
		# Compute 4-digit code in steps of 100 starting at 0100
		seq_val = occ.order_index * 100
		seq_str = f"{seq_val:04d}"
		key = f"{occ.name}___{seq_str}___Segment"
		element_codes = input_details.segment_to_elements.get(occ.name, [])

		segment_map: Dict[str, Dict[str, str]] = {}
		position_index = 0
		for code in element_codes:
			position_index += 1
			# Trim leading zeros; ensure at least one digit remains
			trimmed = str(int(code)) if code.isdigit() else code.lstrip("0") or code
			field_key = f"{trimmed}_{position_index}"
			segment_map[field_key] = {"value": "", "position": f"{position_index:02d}"}

		result[key] = segment_map

	return result


def build_xml_json(
	output_branching: ParsedOutputBranching,
	output_details: ParsedOutputRecordDetails,
) -> Dict[str, object]:
	result: Dict[str, object] = {}
	for elem_name, (min_use, max_use) in output_branching.element_cardinality.items():
		# Build attribute dict using friendly names where available
		friendly_attrs = output_details.element_to_attributes.get(elem_name, [])
		attr_obj = {f"@{attr}": "" for attr in friendly_attrs}

		# If no attributes found, still include empty object to represent element
		if max_use > 1:
			result[elem_name] = [attr_obj]
		else:
			result[elem_name] = attr_obj
	return result


def main() -> None:
	parser = argparse.ArgumentParser(description="Parse EDI schema .txt into edi.json and xml.json")
	parser.add_argument("input", type=Path, help="Path to schema .txt file")
	parser.add_argument("--edi-out", type=Path, default=Path("edi.json"), help="Output path for EDI JSON")
	parser.add_argument("--xml-out", type=Path, default=Path("xml.json"), help="Output path for XML JSON")
	parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"], help="Logging level")
	args = parser.parse_args()

	logging.basicConfig(level=getattr(logging, args.log_level), format="%(levelname)s %(message)s")

	try:
		text = args.input.read_text(encoding="utf-8", errors="ignore")
	except Exception as exc:
		logger.error("Failed to read input file %s: %s", args.input, exc)
		sys.exit(1)

	try:
		parser = SchemaParser(text)
		occurrences = parser.parse_input_branching_segments()
		input_details = parser.parse_input_record_details()
		output_branching = parser.parse_output_branching()
		output_details = parser.parse_output_record_details()

		edi_json = build_edi_json(occurrences, input_details)
		xml_json = build_xml_json(output_branching, output_details)
	except Exception as exc:
		logger.exception("Parsing failed: %s", exc)
		sys.exit(1)

	try:
		args.edi_out.write_text(json.dumps(edi_json, indent=2), encoding="utf-8")
		logger.info("Wrote %s", args.edi_out)
	except Exception as exc:
		logger.error("Failed to write EDI JSON: %s", exc)
		sys.exit(1)

	try:
		args.xml_out.write_text(json.dumps(xml_json, indent=2), encoding="utf-8")
		logger.info("Wrote %s", args.xml_out)
	except Exception as exc:
		logger.error("Failed to write XML JSON: %s", exc)
		sys.exit(1)


if __name__ == "__main__":
	main()