
import csv
import io

# Mock objects (Similar to previous test but focused on Single Product)
class MockProduct:
    def __init__(self):
        self.id = 1
        self.status = 'active'
        self.tags = 'tag'
        self.seo_title = 'seo'
        self.seo_description = 'desc'
        self.option1_name = "Title" # Default for single
        self.option2_name = None
        self.option3_name = None
        self.custom_title = "Single Product"
        self.last_title = "Single Product"
        self.custom_description = "Desc"
        self.custom_vendor = "Vendor"
        self.site = "mercari"
        self.custom_handle = "single-product"
        self.last_status = "active"

class MockVariant:
    def __init__(self):
        self.option1_value = "Default Title" # Default for single
        self.option2_value = None
        self.option3_value = None
        self.sku = "SKU-1"
        self.grams = 100
        self.inventory_qty = 10
        self.price = 1000
        self.taxable = False
        self.country_of_origin = ""
        self.hs_code = ""

def test_single_export():
    product = MockProduct()
    variants = [MockVariant()]
    image_urls = ["http://img1"]
    
    output = io.StringIO()
    # Updated fieldnames
    fieldnames = [
        "Handle", "Title", "Body (HTML)", "Vendor", "Type", "Tags", "Published", 
        "Option1 Name", "Option1 Value",
        "Option2 Name", "Option2 Value",
        "Option3 Name", "Option3 Value",
        "Variant SKU", "Variant Grams", "Variant Inventory Tracker",
        "Variant Inventory Qty", "Variant Inventory Policy", "Variant Fulfillment Service",
        "Variant Price", "Variant Compare At Price", "Variant Requires Shipping", "Variant Taxable",
        "Variant Barcode", "Image Src", "Image Position", "Image Alt Text",
        "Gift Card", "SEO Title", "SEO Description",
        "Google Shopping / Google Product Category", "Google Shopping / Gender", "Google Shopping / Age Group",
        "Google Shopping / MPN", "Google Shopping / AdWords Grouping", "Google Shopping / AdWords Labels",
        "Google Shopping / Condition", "Google Shopping / Custom Product", "Google Shopping / Custom Label 0",
        "Google Shopping / Custom Label 1", "Google Shopping / Custom Label 2", "Google Shopping / Custom Label 3",
        "Google Shopping / Custom Label 4", "Variant Image", "Variant Weight Unit", "Variant Tax Code",
        "Cost per item", "Status"
    ]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()

    handle = "single-handle"

    for i, variant in enumerate(variants):
        row = {f: "" for f in fieldnames}
        row["Handle"] = handle
        row["Status"] = product.status

        if i == 0:
            row["Title"] = "Title"
            row["Body (HTML)"] = "Desc"
            row["Vendor"] = "Vendor"
            row["Type"] = "Mercari Item"
            row["Published"] = "true"
            row["Tags"] = "tag"
            row["SEO Title"] = "seo"
            row["SEO Description"] = "seo_desc"
            if image_urls:
                row["Image Src"] = image_urls[0]
                row["Image Position"] = 1
                row["Image Alt Text"] = "Title"
        
        row["Option1 Name"] = product.option1_name or "Title"
        row["Option2 Name"] = product.option2_name or ""
        row["Option3 Name"] = product.option3_name or ""
        
        row["Option1 Value"] = variant.option1_value
        row["Option2 Value"] = variant.option2_value
        row["Option3 Value"] = variant.option3_value
        row["Variant Price"] = variant.price
        
        writer.writerow(row)

    print(output.getvalue())

if __name__ == "__main__":
    test_single_export()
