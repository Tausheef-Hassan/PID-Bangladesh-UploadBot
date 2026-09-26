# cropper.py
# Finds the horizontal separator between the photograph and the Bengali
# text band, and splits the image there.
#
# FROZEN: tuned over thousands of PID images. Do not edit in passing.
# test_cropper_frozen.py fails on any change; update its hash only when
# the change is deliberate.

import math

import numpy as np


class Cropper:
    def find_white_separator(self, image):
        """Find separator by scanning vertical columns and horizontal lines"""
        height, width = image.shape[:2]
        start_row = int(height * 0.4)

        first_columns = list(range(1, 5))
        last_columns = list(range(width-5, width-1))
        all_columns = first_columns + last_columns

        column_heights = {}

        for col in all_columns:
            column_height = -1
            color_samples = []

            for y in range(height-6, start_row-1, -1):
                pixel = image[y, col]

                if len(color_samples) == 0:
                    color_samples.append(pixel)
                    column_height = y
                else:
                    avg_color = np.mean(color_samples, axis=0)
                    color_diff = np.abs(pixel.astype(np.float32) - avg_color)
                    max_allowed_diff = 255 * 0.02
                    is_matching = np.all(color_diff <= max_allowed_diff)

                    if is_matching:
                        color_samples.append(pixel)
                        column_height = y
                    else:
                        column_heights[col] = height - 1 - y
                        break

            if column_height != -1 and col not in column_heights:
                column_heights[col] = height - 1 - start_row

        if not column_heights:
            return -1, False, 2

        min_uniform_top = height
        for col, col_height in column_heights.items():
            uniform_top_row = height - col_height
            min_uniform_top = min(min_uniform_top, uniform_top_row)

        valid_lines = []

        for first_col in first_columns:
            for last_col in last_columns:
                if first_col in column_heights and last_col in column_heights:
                    height_diff = abs(column_heights[first_col] - column_heights[last_col])
                    if height_diff > 4:
                        continue

                    first_uniform_top = height - column_heights[first_col]
                    last_uniform_top = height - column_heights[last_col]
                    scan_row = max(first_uniform_top, last_uniform_top)

                    if scan_row >= start_row and scan_row < height:
                        row_pixels = image[scan_row, first_col:last_col+1]

                        if len(row_pixels) > 0:
                            line_avg_color = np.mean(row_pixels, axis=0)
                            color_diffs = np.abs(row_pixels.astype(np.float32) - line_avg_color)
                            max_allowed_diff = 255 * 0.02
                            matching_pixels = np.all(color_diffs <= max_allowed_diff, axis=1)
                            matching_percentage = np.sum(matching_pixels) / len(row_pixels)

                            if matching_percentage >= 0.98:
                                valid_lines.append(scan_row)

        # column_heights is non-empty here (checked above), so min_uniform_top is set
        cutoff_row = min(valid_lines) if valid_lines else min_uniform_top

        offset = max(2, round(2 + 3 / math.log(3100 / 670) * math.log(height / 670)))

        separator_row = cutoff_row - offset

        height_38_percent = int(height * 0.38)
        height_42_percent = int(height * 0.42)

        needs_fallback = (separator_row == -1) or (height_38_percent <= cutoff_row <= height_42_percent)

        if needs_fallback:
            fallback_start_row = int(height * 0.75)
            fallback_separator = self.find_separator_fallback(image, fallback_start_row)
            if fallback_separator != -1:
                separator_row = fallback_separator
                return separator_row, True, offset

        return separator_row, False, offset

    @staticmethod
    def _background_fraction(strip):
        """Fraction of a row/column whose pixels are page background (white or #fbf9fa)."""
        strip = strip.astype(np.float32)
        tolerance = 255 * 0.02
        matching = np.zeros(len(strip), dtype=bool)
        for bg_color in ((255, 255, 255), (250, 249, 251)):
            matching |= np.all(np.abs(strip - bg_color) <= tolerance, axis=1)
        return np.sum(matching) / len(strip)

    def find_separator_fallback(self, image, start_row):
        """Fallback method to find separator"""
        height = image.shape[0]

        fallback_consecutive_similar_lines = 0
        fallback_separator_row = -1

        fallback_required_lines = round((4 / math.log(3100 / 670)) * math.log(height / 670) + 5)
        if fallback_required_lines <= 1:
            fallback_required_lines = 2

        for y_fallback in range(start_row, height):
            if self._background_fraction(image[y_fallback]) >= 0.98:
                fallback_consecutive_similar_lines += 1
                if fallback_consecutive_similar_lines >= fallback_required_lines:
                    n = fallback_consecutive_similar_lines
                    fallback_separator_row = y_fallback - (2 if n <= 3 else n + 5)
                    break
            else:
                fallback_consecutive_similar_lines = 0

        return fallback_separator_row

    def crop_side_whitespace(self, image):
        """Crop white or fbf9fa colored sections from left and right sides"""
        height, width = image.shape[:2]

        left_crop = 0
        for x in range(width):
            if self._background_fraction(image[:, x]) < 0.98:
                break
            left_crop = x + 1

        right_crop = width
        for x in range(width - 1, -1, -1):
            if self._background_fraction(image[:, x]) < 0.98:
                break
            right_crop = x

        expansion = int(round((4 / math.log(3100 / 670)) * math.log(height / 670) + 5))
        left_expanded = max(0, left_crop - expansion)
        right_expanded = min(width, right_crop + expansion)

        if left_expanded < right_expanded:
            return image[:, left_expanded:right_expanded]
        else:
            return image

    def crop_image_sections(self, image, separator_row, apply_side_crop=False):
        """Split image into photo section and text section"""
        if apply_side_crop:
            image = self.crop_side_whitespace(image)

        if separator_row == -1:
            return None, image

        photo_section = image[:separator_row, :]
        text_section = image[separator_row:, :]

        if photo_section is None or photo_section.size == 0 or photo_section.shape[0] < 1:
            return None, image

        return photo_section, text_section
